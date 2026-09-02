"""The desk's two swing budgets (SW11, STANDING-ANSWERS B9), measured and recorded.

* tick → verdict < 5 ms in-process (`docs/swing/06` SW11);
* the confirm path — lock, re-size, order, GTT — < 2 s in DRY_RUN.

Each is recorded through ``benchmarks.budgets.record`` in the data plant's tree, so
``benchmarks/AS-MEASURED.md`` carries the number beside the docs/11 rows. The desk has no
``benchmarks`` package of its own; the module is stdlib-only and is imported off the path.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import statistics
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "decile-blueprint") not in sys.path:
    sys.path.insert(0, str(ROOT / "decile-blueprint"))

from benchmarks.budgets import BUDGET_BY_KEY, record  # noqa: E402 - after the path

from app import swing_execute as X  # noqa: E402
from app.core.risk import RiskManager  # noqa: E402
from app.strategies.swing_breakout import SwingBreakout  # noqa: E402
from baskfy_core.swing.opening_range import TriggerState  # noqa: E402
from tests.test_swing_execute import NOW, ExplodingKC, MemoryStore  # noqa: E402
from tests.test_swing_monitor import ListStore, NoCandles, _name, _tick  # noqa: E402

pytestmark = pytest.mark.benchmark

D = Decimal
TICKS = 2_000


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(0.95 * len(ordered)) - 1))]


def test_swing_tick_to_verdict_p95_is_under_five_milliseconds() -> None:
    """Twenty names, two thousand ticks after the range closed, every one a verdict."""
    names = [_name(f"N{i}", i + 1, pivot="100") for i in range(20)]
    strategy = SwingBreakout(None, watchlist=names, store=ListStore(), candles=NoCandles(),
                             day=dt.date(2026, 8, 19))
    warm = [_tick(n.token, 99.0, "09:15") for n in names] + \
           [_tick(n.token, 99.5, "09:19") for n in names] + \
           [_tick(n.token, 99.4, "09:20") for n in names]

    async def go() -> None:
        for t in warm:
            await strategy.on_tick(t)
        strategy.verdict_seconds.clear()
        for i in range(TICKS):
            token = names[i % len(names)].token
            await strategy.on_tick(_tick(token, 99.0 + (i % 7) / 100, "09:25"))

    asyncio.run(go())
    assert len(strategy.verdict_seconds) == TICKS
    assert all(s.last_state is TriggerState.WAITING for s in strategy.state.values())
    p95_ms = _p95(strategy.verdict_seconds) * 1000
    budget = BUDGET_BY_KEY["swing_tick_to_verdict"]
    assert p95_ms < budget.limit, f"tick→verdict p95 {p95_ms:.3f} ms ≥ {budget.limit} ms"
    record(
        "swing_tick_to_verdict", round(p95_ms, 4), unit="ms",
        method=f"p95 of {TICKS} in-process `SwingBreakout.on_tick` calls over 20 names after "
               f"the 5-minute range closed (median {statistics.median(strategy.verdict_seconds) * 1000:.3f} ms)",
        dataset="synthetic ticks; no bus, no broker, no database — the strategy alone",
    )


def test_swing_confirm_path_is_under_two_seconds_in_dry_run(monkeypatch, tmp_path) -> None:
    """Lock → re-size → LIMIT order → GTT, through the real swing gateway in its dry-run
    branch over an exploding broker client, in-memory store. Twenty confirms; p95 and max."""
    from app import config as C

    monkeypatch.setattr(C, "SWING_EXECUTION_ENABLED", False)
    monkeypatch.setattr(C, "DRY_RUN", True)
    monkeypatch.chdir(tmp_path)
    gw = X.build_swing_gateway(ExplodingKC(), RiskManager())
    store = MemoryStore(capital=D("100000000"))
    took: list[float] = []
    for i in range(20):
        # One confirm per session day: the three-entries-a-session cap (`04` §5.3) is a real
        # rule, and the position is closed after each so the count never reaches the tier's.
        now = NOW + dt.timedelta(days=i)
        plan_id = store.add_plan(built_at=now - dt.timedelta(minutes=5))
        line_id = store.add_line(plan_id, symbol=f"S{i}", instrument_id=1000 + i)
        store.detected[f"S{i}"] = (D("5.00"), D(100_000_000), D("72.00"))
        started = time.perf_counter()
        out = asyncio.run(X.execute_line(store, gw, plan_id=plan_id, line_id=line_id,
                                         confirm="true", now=now))
        took.append(time.perf_counter() - started)
        assert out.status == "SIMULATED" and out.gtt is not None, out
        store.positions[out.position_id].update({"state": "CLOSED", "quantity_open": 0})
    p95_s = _p95(took)
    budget = BUDGET_BY_KEY["swing_confirm_path"]
    assert max(took) < budget.limit, f"a confirm took {max(took):.3f} s ≥ {budget.limit} s"
    record(
        "swing_confirm_path", round(p95_s, 4), unit="s",
        method=f"p95 of 20 `execute_line` confirms (max {max(took):.4f} s): session lock, "
               f"re-size, LIMIT buy, GTT, rows",
        dataset="DRY_RUN through the real swing gateway over an exploding broker client and "
                "an in-memory store — no network, no Postgres",
    )
