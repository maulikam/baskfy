#!/usr/bin/env python
"""Measure the options book's two in-process budgets and print them (OP14).

    cd kite-momentum-rebalancer
    .venv/bin/python ../tools/options/budgets.py

`docs/options/06` OP14: "Budgets measured and recorded: tick -> mark -> decision latency, plan
build time, the limiter share with swing, TWT and VBT mornings running."

* **tick -> mark -> decision**: every tick of each recorded fixture day (`tools/options/fixtures`)
  handed to the desk's real `NiftyOptionsMonitor.on_tick`, which marks the leg or builds the bar and
  evaluates every open position's exit, timed per tick (the store is in memory, so this is the
  monitor's own cost, not the database's).
* **plan build**: one Beat minute of each sleeve's plan builder (`decide_o1` / `decide_o2` /
  `decide_o3` through `replay_day.decide_plan`) at its decision minute over the drill's fixture
  chain, timed per call.
* **the limiter share** is not measurable here: it needs the swing, TWT and VBT mornings running on
  the box against Kite. The design share is printed beside it — the collector's one quote call a
  minute and the index bars' one historical call a minute against the families' budgets.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import importlib.util
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


def _load(name: str, path: Path):  # noqa: ANN202 - a module
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


replay = _load("options_replay", HERE / "replay.py")
drill = _load("options_drill", HERE / "drill.py")

from app.strategies.nifty_options import NiftyOptionsMonitor  # noqa: E402
from baskfy_core.options.replay_day import decide_plan  # noqa: E402
from baskfy_core.options.scan import DayContext  # noqa: E402


def _ms(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "n": len(ordered),
        "p50_ms": round(statistics.median(ordered) * 1000, 3),
        "p99_ms": round(ordered[max(0, int(len(ordered) * 0.99) - 1)] * 1000, 3),
        "max_ms": round(ordered[-1] * 1000, 3),
    }


def tick_latency() -> dict[str, float]:
    samples: list[float] = []
    for fixture_path in sorted((HERE / "fixtures").glob("*.json")):
        if fixture_path.name.endswith(".expected.json"):
            continue
        fixture = json.loads(fixture_path.read_text())
        store = replay.ListStore([replay.position_of(fixture["position"])], [])
        monitor = NiftyOptionsMonitor(None, store=store, day=dt.date.fromisoformat(fixture["day"]))

        async def run(monitor: NiftyOptionsMonitor = monitor, fixture: dict = fixture) -> None:
            await monitor.on_start()
            for tick in replay.ticks_of(fixture):
                began = time.perf_counter()
                await monitor.on_tick(tick)
                samples.append(time.perf_counter() - began)

        asyncio.run(run())
    return _ms(samples)


def plan_build() -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for sleeve, day, bars in drill.DAYS:
        smile = drill.F.skew(0.30, 0.33) if sleeve.value.startswith("O1") else drill.F.flat(0.14)
        chain = drill.FixtureChain(day, bars, smile)
        market = drill.F.market(day, bars)
        decided = decide_plan(sleeve, market, DayContext(), chain, drill.OPTIONS, drill.CEILINGS)
        samples: list[float] = []
        for _ in range(20):
            fresh = drill.FixtureChain(day, bars, smile)
            began = time.perf_counter()
            decide_plan(sleeve, market, DayContext(), fresh, drill.OPTIONS, drill.CEILINGS)
            samples.append(time.perf_counter() - began)
        minutes = 1
        if isinstance(decided, drill.Decided):
            first = dt.datetime.combine(day, dt.time(10, 0) if sleeve.value.startswith("O1")
                                        else dt.time(9, 30), drill.IST)  # fmt: skip
            minutes = max(1, int((decided.decision_minute - first).total_seconds() // 60) + 1)
        whole = _ms(samples)
        out[sleeve.value] = {**whole, "minutes_searched": minutes,
                             "per_minute_ms": round(whole["p50_ms"] / minutes, 3)}  # fmt: skip
    return out


def main() -> int:
    budgets = {
        "tick_to_decision": tick_latency(),
        "plan_build": plan_build(),
        "limiter_share_design_pct": {"quote": round(100 * 1 / 60, 2),
                                     "historical": round(100 * 1 / 180, 2)},  # fmt: skip
        "limiter_share_measured": "box-only: needs the swing, TWT and VBT mornings running (⛁)",
    }
    print(json.dumps(budgets, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
