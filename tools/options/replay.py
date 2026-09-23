#!/usr/bin/env python
"""Replay a recorded session through the options monitor and say what it raised (OP9).

`docs/options/06` OP9: "`tools/options/replay.py` — a recorded day (minute CSV + snapshot series)
through the process, asserting every plan and exit decision."

    cd kite-momentum-rebalancer
    .venv/bin/python ../tools/options/replay.py ../tools/options/fixtures/o1-profit.json \\
        --expect ../tools/options/fixtures/o1-profit.expected.json

A fixture (`make_fixtures.py` writes them) is one JSON file: the day, one open position as
`op_position` and its plan carry it, NIFTY 50's minute bars, and each leg's bid/ask each minute.
Exit code 0 when the exits match the expectation exactly (session, code, reason, time), 1 when they
do not; the diff is printed either way.

HOW A SESSION BECOMES TICKS. Each index minute becomes four ticks — open, low, high, close at +0,
+15, +30 and +45 seconds (the swing replay's convention) — and each leg quote one depth tick at +50
seconds. `feed_stops` drops every index tick from that moment on, which is how the feed "dies at
14:05". `start` replays a restart: the monitor is built at `start` with the position already in
the store, ticks before it are not delivered, and the bars before it come back through the store's
`index_minutes` (what `op_index_minute` would hold) — the process resumes from `op_position`.

WHAT IT PROVES. The strategy is driven with no broker, no bus and no database: the store is a list.
The verdicts are `baskfy_core.options.exits.evaluate`'s, so a replay that raises the expected exit
at the expected second is the whole chain from tick to exit plan, minus only the wire.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DESK = ROOT / "kite-momentum-rebalancer"
if str(DESK) not in sys.path:
    sys.path.insert(0, str(DESK))

from baskfy_core.options.bars import Bar  # noqa: E402
from baskfy_core.options.config import Sleeve  # noqa: E402
from baskfy_core.options.execution import LegRole  # noqa: E402
from baskfy_core.options.exits import ExitVerdict, OpenLeg, OpenPosition  # noqa: E402
from baskfy_core.options.structures import Direction, Structure  # noqa: E402

from app.strategies.nifty_options import (  # noqa: E402
    NIFTY_50_TOKEN,
    NiftyOptionsMonitor,
    TrackedPosition,
)


def _d(value: object) -> Decimal | None:
    return None if value in (None, "") else Decimal(str(value))


def position_of(raw: dict) -> TrackedPosition:
    """The fixture's position as the store would hand it to the monitor."""
    legs = tuple(
        OpenLeg(
            role=LegRole(lg["role"]),
            strike=Decimal(lg["strike"]),
            quantity=int(lg["quantity"]),
            fill_price=Decimal(lg["fill_price"]),
            instrument_token=int(lg["instrument_token"]),
        )
        for lg in raw["legs"]
    )
    return TrackedPosition(
        session_id=int(raw["session_id"]),
        position=OpenPosition(
            sleeve=Sleeve(raw["sleeve"]),
            structure=Structure(raw["structure"]),
            legs=legs,
            entry_points=Decimal(raw["entry_points"]),
            opened_at=dt.datetime.fromisoformat(raw["opened_at"]),
            risk_budget_inr=Decimal(raw["risk_budget_inr"]),
            direction=Direction(raw["direction"]) if raw.get("direction") else None,
            range_high=_d(raw.get("range_high")),
            range_low=_d(raw.get("range_low")),
            half_gap=_d(raw.get("half_gap")),
            width_points=_d(raw.get("width_points")),
        ),
    )


def bars_of(fixture: dict) -> list[Bar]:
    return [
        Bar(
            ts=dt.datetime.fromisoformat(t),
            open=Decimal(o),
            high=Decimal(h),
            low=Decimal(low),
            close=Decimal(c),
        )
        for t, o, h, low, c in fixture["index"]
    ]


def ticks_of(fixture: dict) -> list[dict]:
    """The session as ticks, in delivery order (index before leg within the same second)."""
    stops = fixture.get("feed_stops")
    stop_at = dt.datetime.fromisoformat(stops) if stops else None
    out: list[tuple[dt.datetime, int, dict]] = []
    for t_iso, o, h, low, c in fixture["index"]:
        t = dt.datetime.fromisoformat(t_iso)
        for sec, price in ((0, o), (15, low), (30, h), (45, c)):
            at = t + dt.timedelta(seconds=sec)
            if stop_at is None or at < stop_at:
                out.append(
                    (at, 0, {"instrument_token": NIFTY_50_TOKEN, "last_price": price,
                             "exchange_timestamp": at})
                )
    for t_iso, token, bid, ask in fixture["legs"]:
        at = dt.datetime.fromisoformat(t_iso) + dt.timedelta(seconds=50)
        depth = {"buy": [{"price": bid, "quantity": 1300}], "sell": [{"price": ask, "quantity": 1300}]}
        out.append((at, 1, {"instrument_token": int(token), "depth": depth, "exchange_timestamp": at}))
    return [tick for _, _, tick in sorted(out, key=lambda e: (e[0], e[1]))]


@dataclass
class ListStore:
    """The monitor's store as lists: the positions it holds, every exit and mark it recorded."""

    positions: list[TrackedPosition]
    minutes: list[Bar]
    exits: list[tuple[int, str, str, dt.datetime]] = field(default_factory=list)
    marks: list[tuple[int, Decimal, dt.datetime]] = field(default_factory=list)

    def open_positions(self, day: dt.date) -> list[TrackedPosition]:
        del day
        return list(self.positions)

    def raise_exit(self, tracked: TrackedPosition, verdict: ExitVerdict, at: dt.datetime) -> str:
        self.exits.append((tracked.session_id, verdict.code, verdict.reason, at))
        plan_id = f"EXIT-{tracked.session_id}"
        self.positions = [
            TrackedPosition(p.session_id, p.position, plan_id)
            if p.session_id == tracked.session_id
            else p
            for p in self.positions
        ]
        return plan_id

    def record_mark(self, tracked: TrackedPosition, value: Decimal, at: dt.datetime) -> None:
        self.marks.append((tracked.session_id, value, at))

    def index_minutes(self, day: dt.date, until: dt.datetime) -> list[Bar]:
        del day
        return [b for b in self.minutes if b.ts + dt.timedelta(minutes=1) <= until]


def replay(fixture: dict) -> tuple[NiftyOptionsMonitor, ListStore]:
    """Drive the monitor through the fixture. A restart (`start`) sees only the ticks after it,
    and the store serves the morning's bars as `op_index_minute` would."""
    day = dt.date.fromisoformat(fixture["day"])
    start = dt.datetime.fromisoformat(fixture["start"]) if fixture.get("start") else None
    stored = [b for b in bars_of(fixture) if start is not None and b.ts < start]
    store = ListStore([position_of(fixture["position"])], stored)
    monitor = NiftyOptionsMonitor(None, store=store, day=day)

    async def run() -> None:
        await monitor.on_start()
        for tick in ticks_of(fixture):
            if start is not None and tick["exchange_timestamp"] < start:
                continue
            await monitor.on_tick(tick)

    asyncio.run(run())
    return monitor, store


def as_json(exits: list[tuple[int, str, str, dt.datetime]]) -> list[dict[str, Any]]:
    return [
        {"session_id": sid, "code": code, "reason": reason, "at": at.isoformat()}
        for sid, code, reason, at in exits
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="replay", description=__doc__.split("\n")[0])
    parser.add_argument("fixture")
    parser.add_argument("--expect", help="the expected exits (JSON); exit 1 on a mismatch")
    args = parser.parse_args(argv)
    fixture = json.loads(Path(args.fixture).read_text())
    _, store = replay(fixture)
    got = as_json(store.exits)
    print(json.dumps(got, indent=1))
    if args.expect is None:
        return 0
    want = json.loads(Path(args.expect).read_text())
    if got == want:
        print("OK: the exits match the expectation")
        return 0
    print("MISMATCH\n  expected: " + json.dumps(want) + "\n  got:      " + json.dumps(got))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
