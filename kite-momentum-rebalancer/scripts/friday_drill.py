"""The Friday drill, end to end, with zero orders — M21 §2.

    make drill                      (from decile-blueprint/)
    .venv/bin/python -m scripts.friday_drill --date 2026-08-18

Generate a scan, build a plan, execute it, preview the stops. The whole loop the desk runs on a
Friday afternoon, with `DRY_RUN=true` so nothing reaches a broker.

IT REFUSES TO RUN WITH DRY_RUN OFF
------------------------------------
Not a warning, not a prompt — a refusal, before anything is built. A drill that can place a real
order is not a drill, and the difference between the two is one environment variable that somebody
will eventually have set for a real session and forgotten.

WITHOUT A KITE TOKEN IT STILL RUNS, AND SAYS SO
------------------------------------------------
(Unless `--require-live` is given, which is the flag for a *scheduled* drill: it exits 2, the
desk's shared "the token has expired" code, so an unattended run is triageable from launchctl
output rather than from a traceback.)

`/analyze` needs holdings and cash, which need a live session, and Kite tokens die every morning
(`NEEDS-MAULIK.md` item 3). Rather than being un-runnable most of the time, the drill substitutes a
clearly-labelled stub book and prints **STUB BOOK** in its header and its verdict. What it proves
in that mode is that the machinery works end to end; what it cannot prove is anything about the
real portfolio. Both facts are on the output, so a green run can never be mistaken for more than
it is.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

ROOT = Path(__file__).resolve().parent.parent

OK = 200

#: Statuses that mean the order never reached even the simulated placement path.
_NOT_ATTEMPTED = frozenset({"BLOCKED", "RISK_BLOCKED", "ABORTED", "None", "UNKNOWN"})


class RefusingBroker:
    """The `.kc` handle the gateway constructs with. Every attribute is a refusal.

    Under `DRY_RUN=true` the gateway simulates and never reaches this. It exists for the case
    where it *would* — a drill that quietly acquired the ability to trade because someone changed
    one variable is the failure this whole script is shaped to prevent.
    """

    def __getattr__(self, name: str) -> Callable[..., NoReturn]:
        def refuse(*_a: object, **_k: object) -> NoReturn:
            raise RuntimeError(
                f"the drill's stub broker was asked to {name}(). Nothing in a drill may reach a "
                f"broker; if DRY_RUN is off, that is the bug."
            )

        return refuse


class StubBook:
    """A book, when there is no session. Every method is a read; none can reach a broker."""

    def __init__(self, prices: dict[str, float] | None = None) -> None:
        self.prices = prices or {}
        self.kc = RefusingBroker()
        self._book, self._cash = _last_snapshot_book()

    def is_authed(self) -> bool:
        return True

    def holdings(self) -> list[dict[str, Any]]:
        """The desk's last recorded book, not an empty one.

        An empty book was the first version and it made the drill useless: `/execute` measures the
        daily loss cap as `book_value - last_invested`, so a stub holding nothing against a
        database recording Rs 1.04 crore invested looks like a total loss, trips the cap, and
        every order comes back RISK_BLOCKED. The drill was "green" — no orders reached a broker —
        while exercising none of the path it exists to exercise.
        """
        return self._book

    def available_cash(self) -> float:
        return self._cash

    def ltp(self, symbols: list[str]) -> dict[str, float]:
        """Real closes for the drill's date, not a nominal number.

        Quantity is `capital x weight / price`, so a made-up price makes every position size
        meaningless and the drill stops exercising the part most worth exercising.
        """
        return {s: self.prices[s] for s in symbols if s in self.prices}

    def positions(self) -> dict[str, list]:
        return {"net": [], "day": []}

    def margins(self) -> dict[str, Any]:
        return {"equity": {"available": {"live_balance": 1_000_000.0}}}


def _last_snapshot_book() -> tuple[list[dict[str, Any]], float]:
    """The positions and cash from the desk's most recent EOD snapshot."""
    import json  # noqa: PLC0415

    from app.analytics import db  # noqa: PLC0415

    with db.connect() as conn:
        row = conn.execute(
            "select holdings_json, cash from snapshots order by date desc limit 1"
        ).fetchone()
    if row is None:
        return [], 1_000_000.0
    payload = row["holdings_json"]
    parsed = json.loads(payload) if isinstance(payload, str) else payload
    positions = [
        {
            "symbol": p["symbol"],
            "quantity": int(p["quantity"]),
            "pledged_qty": int(p.get("pledged_qty") or 0),
            "last_price": float(p.get("price") or 0.0),
            "average_price": float(p.get("average_price") or 0.0),
        }
        for p in parsed.get("positions", [])
    ]
    return positions, float(row["cash"] or 0.0)


def _closes(as_of: dt.date) -> dict[str, float]:
    """That session's closing prices, straight from the screener's bars."""
    import psycopg  # noqa: PLC0415
    from app import scan_source  # noqa: PLC0415

    with psycopg.connect(scan_source.SCREENER_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "select i.symbol, b.close from ohlcv_daily b "
            "join instrument i on i.id = b.instrument_id where b.date = %(on)s",
            {"on": as_of},
        )
        return {row[0]: float(row[1]) for row in cur.fetchall() if row[1]}


def main() -> int:  # noqa: PLR0912, PLR0915 — a linear script; splitting it would hide the loop
    sys.path.insert(0, str(ROOT))
    from app import config as C  # noqa: PLC0415
    from app import main as M  # noqa: PLC0415
    from fastapi.testclient import TestClient  # noqa: PLC0415

    if not C.DRY_RUN:
        raise SystemExit(
            "DRY_RUN is false. This drill will not run against a live order path.\n"
            "Set DRY_RUN=true in .env, or run the real session deliberately from the web UI."
        )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="the Friday, YYYY-MM-DD")
    parser.add_argument(
        "--require-live",
        action="store_true",
        help="fail with exit 2 rather than falling back to a stub book. For a scheduled run.",
    )
    args = parser.parse_args()
    as_of = dt.date.fromisoformat(args.date)

    live = M.kite()
    authed = False
    try:
        authed = bool(live.is_authed())
    except Exception:  # a broken session is a stub book, not a crashed drill
        authed = False

    if not authed and args.require_live:
        # Exit 2 is the desk's "the Kite token has expired" code, shared by every script that can
        # hit one, so an unattended run is triageable from launchctl output alone rather than by
        # reading a traceback. `tests/test_exit_codes.py` enforces it across scripts/.
        print("Friday drill: no Kite session, and --require-live was given.")
        return 2

    mode = "LIVE BOOK" if authed else "STUB BOOK"
    if not authed:
        stub = StubBook(_closes(as_of))
        M.kite = lambda: stub  # type: ignore[assignment]

    print(f"Friday drill — {as_of}   DRY_RUN={C.DRY_RUN}   {mode}")
    print("=" * 72)

    # The desk refuses a state-changing POST without an Origin it recognises — that is what stops
    # a form on another site posting to it while the SSH tunnel is open. A browser always sends
    # one; a TestClient does not unless told, so the drill sends what a browser would rather than
    # the protection being disabled for it.
    client = TestClient(M.app, headers={"Origin": "http://testserver:8420"})
    failures: list[str] = []

    # --- 1. generate the scan and build a plan --------------------------------------------
    response = client.post("/analyze", data={"generate_for": as_of.isoformat()})
    if response.status_code != OK:
        print(f"  1. analyze            FAILED {response.status_code}: {response.text[:200]}")
        return 1
    plan = response.json()
    orders = [o for o in plan["orders"] if o.get("delta")]
    print(f"  1. analyze            ok    plan {plan['plan_id']}, {len(orders)} orders")
    print(f"     scan               {plan['scan']['source']}  {plan['scan']['screen_run_id']}")
    for warning in plan["scan"].get("warnings", []):
        print(f"     WARNING            {warning[:150]}")
    print(f"     breadth            {plan['breadth']['value']} from {plan['breadth']['source']}")

    # --- 2. review, the way a human would --------------------------------------------------
    buys = [o for o in orders if o["delta"] > 0]
    sells = [o for o in orders if o["delta"] < 0]
    pledged = [o["symbol"] for o in orders if o.get("pledged")]
    print(
        f"  2. review             ok    {len(buys)} buys, {len(sells)} sells, "
        f"{len(pledged)} pledged"
    )

    # --- 3. execute, under DRY_RUN ---------------------------------------------------------
    executed = client.post(
        "/execute", data={"plan_id": plan["plan_id"], "confirm": "true", "place_stops": "true"}
    )
    if executed.status_code != OK:
        print(f"  3. execute            FAILED {executed.status_code}: {executed.text[:200]}")
        return 1
    result = executed.json()
    if not result.get("dry_run"):
        # Belt and braces. The refusal above should make this unreachable; if it is ever reached,
        # real orders have just been placed and that must be the loudest line in the output.
        print("  3. execute            *** THE RESPONSE SAYS DRY_RUN IS OFF ***")
        return 1
    statuses = sorted({str(o.get("status")) for o in result["orders"]})
    print(f"  3. execute            ok    dry_run=True, {len(result['orders'])} results {statuses}")

    # --- 4. the stops preview --------------------------------------------------------------
    stops = client.get("/stops")
    print(
        f"  4. stops preview      {'ok   ' if stops.status_code == OK else 'FAILED'} "
        f"{stops.status_code}   ({result['stops_note'][:60]}...)"
    )
    if stops.status_code != OK:
        failures.append("stops")

    # --- 5. the thing the whole drill is for -----------------------------------------------
    journal = ROOT / "data" / "outputs" / "orders_journal.jsonl"
    real = 0
    if journal.exists():
        import json  # noqa: PLC0415

        for line in journal.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("plan_id", "").startswith(plan["plan_id"]) and not entry.get("dry_run"):
                real += 1
    print(f"  5. orders that reached a broker: {real}")
    if real:
        failures.append("real orders were placed")

    print("=" * 72)
    verdict = "DRILL GREEN" if not failures else f"DRILL RED: {', '.join(failures)}"
    print(f"{verdict}   ({mode})")
    if not authed:
        print("  This ran against a stub book. It proves the machinery works end to end.")
        print("  It proves nothing about the real portfolio — that needs a Kite login.")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
