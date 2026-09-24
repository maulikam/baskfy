"""FO10: ``FNO_WEEKLY`` — the FO book's week, one line per ``(sleeve, simulated)``, never pooled.

The options pack's OP11 precedent (``OPTIONS_WEEKLY``). Pure over the ledger's figures, and dark
unless ``BASKFY_FNO_MONITOR_ENABLED``.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from baskfy_core.fno.config import FoSleeve
from baskfy_core.fno.ledger import ClosedTrade, build_ledger, evaluate_pauses
from baskfy_worker.alerts import AlertName
from baskfy_worker.celery_app import BEAT_SCHEDULE
from baskfy_worker.fno.weekly import week_lines, weekly_alert
from baskfy_worker.settings import get_worker_settings
from baskfy_worker.tasks import celery_tasks

FRIDAY = dt.date(2026, 10, 30)


def closed(  # noqa: PLR0913 - one journal row
    n: int, sleeve: FoSleeve, r: str, net: str, *, simulated: bool, on: dt.date
) -> ClosedTrade:
    return ClosedTrade(n, sleeve, "NIFTY" if sleeve is FoSleeve.F1N else "ZQA", simulated, on,
                       None, Decimal(net), Decimal(r), "LOSS_CLOSE")  # fmt: skip


def test_one_line_per_pool_and_the_books_apart() -> None:
    trades = [
        closed(1, FoSleeve.F1N, "0.40", "10000", simulated=True, on=FRIDAY),
        closed(2, FoSleeve.F1N, "-0.70", "-17500", simulated=True, on=FRIDAY),
        closed(3, FoSleeve.F1N, "-1.00", "-25000", simulated=False, on=FRIDAY),
        closed(4, FoSleeve.F2, "1.50", "3000", simulated=True, on=FRIDAY),
        closed(5, FoSleeve.F2, "-1", "-99", simulated=True, on=FRIDAY - dt.timedelta(days=7)),
    ]
    assert week_lines(trades, FRIDAY) == [
        "F1N paper: 2 closed, ₹-7500, -0.30R (worst -0.70R)",
        "F2 paper: 1 closed, ₹3000, 1.50R (worst 1.50R)",
        "F1N LIVE: 1 closed, ₹-25000, -1.00R (worst -1.00R)",
    ]
    pauses = {
        s: evaluate_pauses(trades, simulated=s, as_of=FRIDAY, monthly_pause_inr=Decimal(20000))
        for s in (True, False)
    }
    alert = weekly_alert(trades, build_ledger(trades, [], FRIDAY), pauses, FRIDAY)
    assert alert.name is AlertName.FNO_WEEKLY
    assert "paper book month to date: 4 closed, ₹-4599" in alert.summary
    assert "LIVE book month to date: 1 closed, ₹-25000" in alert.summary
    # The live loss reaches a ₹20,000 book limit; paper's -₹4,599 does not.
    found = alert.detail["pauses"]
    assert isinstance(found, list)
    assert [(p["simulated"], p["scope"]) for p in found if isinstance(p, dict)] == [(False, "BOOK")]


def test_a_quiet_week_says_so() -> None:
    alert = weekly_alert([], build_ledger([], [], FRIDAY), {}, FRIDAY)
    assert "no FO structure closed this week" in alert.summary


def test_dark_behind_the_monitor_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BASKFY_FNO_MONITOR_ENABLED", raising=False)
    get_worker_settings.cache_clear()
    try:
        out = celery_tasks.fno_weekly_task.run(at="2026-10-30T16:35:00+05:30")
    finally:
        get_worker_settings.cache_clear()
    assert out["skipped"] == "BASKFY_FNO_MONITOR_ENABLED is false"
    entry = BEAT_SCHEDULE["fno-weekly"]
    assert entry["task"] == celery_tasks.FNO_WEEKLY_TASK
