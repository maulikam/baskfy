"""OP11 — the weekly summary and the first-live multiplier on a database (`docs/options/06` OP11).

* "no summary pools sleeves, simulated, or sizing modes" — `OPTIONS_WEEKLY` is one line per pool
  (`TestTheWeeklyAlert`), read from the week's journal and skipped sessions (`TestOnADatabase`);
* "five real rows lift `half_size`" — the context the plan builders size from counts real journal
  rows, and the fifth lifts the half size (`TestTheFirstLiveMultiplier`).
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from test_options_scan_task import (
    _rolled_back,
    op_url,  # noqa: F401 - the module-scoped migration fixture
    requires_db,
)

from baskfy_core.models import AppUser, OpJournal, OpSession, OpSleeveConfig
from baskfy_core.options.config import (
    Mode,
    OptionsCeilings,
    OptionsConfig,
    SizingMode,
    Sleeve,
)
from baskfy_core.options.journal import JournalRow
from baskfy_core.options.structures import size_for
from baskfy_worker.alerts import AlertName
from baskfy_worker.options.scan import load_context
from baskfy_worker.options.weekly import week_rows, weekly_alert

FRIDAY = dt.date(2026, 10, 30)


def traded(sleeve: Sleeve, *, simulated: bool, net: str, mode: SizingMode) -> JournalRow:
    return JournalRow(
        sleeve=sleeve, trade_date=FRIDAY, simulated=simulated, sizing_mode=mode, traded=True,
        net_pnl_inr=Decimal(net), r_inr=Decimal("1000"), closed_reason="TARGET", minutes_held=30,
    )  # fmt: skip


class TestTheWeeklyAlert:
    def test_one_line_per_pool_never_pooled(self) -> None:
        rows = [
            traded(Sleeve.O2, simulated=True, net="500", mode=SizingMode.PAPER_ONE_LOT),
            traded(Sleeve.O2, simulated=False, net="-1000", mode=SizingMode.BUDGET),
            traded(Sleeve.O3A, simulated=True, net="200", mode=SizingMode.PAPER_ONE_LOT),
        ]
        alert = weekly_alert(rows, FRIDAY)
        assert alert.name is AlertName.OPTIONS_WEEKLY
        assert alert.labels["pools"] == "3"
        assert "O2 paper PAPER_ONE_LOT: 1 traded; win 100%, mean 0.50R" in alert.summary
        assert "O2 LIVE BUDGET: 1 traded; win 0%, mean -1.00R" in alert.summary
        assert "O3A paper PAPER_ONE_LOT: 1 traded" in alert.summary
        # No number anywhere mixes the paper and the live O2 rows.
        assert "O2: 2 traded" not in alert.summary

    def test_a_week_with_nothing_says_so(self) -> None:
        assert "no options session was decided this week" in weekly_alert([], FRIDAY).summary


class TestTheGate:
    def test_dark_with_the_monitor_flag_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from baskfy_worker.settings import WorkerSettings  # noqa: PLC0415
        from baskfy_worker.tasks import celery_tasks  # noqa: PLC0415

        def no_db(operation: object) -> object:
            raise AssertionError(f"a database session was opened for {operation!r}")

        monkeypatch.setattr(celery_tasks, "run_in_session", no_db)
        monkeypatch.setattr(celery_tasks, "sole_user_id", lambda: 1)
        monkeypatch.setattr(
            celery_tasks, "get_worker_settings", lambda: WorkerSettings(_env_file=None)
        )
        out = celery_tasks.options_weekly_task.run("2026-10-30T16:30:00+05:30")
        assert out["skipped"] == "BASKFY_OPTIONS_MONITOR_ENABLED is false"

    def test_beat_fridays_after_the_close(self) -> None:
        from baskfy_worker.celery_app import BEAT_SCHEDULE  # noqa: PLC0415

        entry = BEAT_SCHEDULE["options-weekly"]
        assert entry["task"] == "baskfy.options.weekly"


async def _user(session: AsyncSession) -> int:
    user = AppUser(
        public_id=f"op11-{uuid.uuid4().hex[:8]}", email=f"op11-{uuid.uuid4().hex[:8]}@x.test"
    )
    session.add(user)
    await session.flush()
    return int(user.id)


async def _journal(session: AsyncSession, user_id: int, day: dt.date, *, simulated: bool) -> None:
    mode = "PAPER" if simulated else "LIVE"
    row = OpSession(user_id=user_id, sleeve="O2", trade_date=day, mode=mode, state="CLOSED")
    session.add(row)
    await session.flush()
    session.add(
        OpJournal(
            session_id=row.id,
            user_id=user_id,
            sleeve="O2",
            trade_date=day,
            expiry_used=day,
            structure="LONG_OPTION",
            entry_inr=Decimal(1000),
            exit_inr=Decimal(1500),
            gross_pnl_inr=Decimal(500),
            costs_inr=Decimal(0),
            net_pnl_inr=Decimal(500),
            risk_budget_inr=Decimal(1000),
            r_multiple=Decimal("0.5"),
            closed_reason="TARGET",
            minutes_held=30,
            simulated=simulated,
            sizing_mode="PAPER_ONE_LOT" if simulated else "BUDGET",
        )
    )
    await session.flush()


@requires_db
class TestOnADatabase:
    async def test_the_week_reads_journal_rows_and_skipped_sessions(self, op_url: str) -> None:  # noqa: F811
        async with _rolled_back(op_url) as session:
            uid = await _user(session)
            await _journal(session, uid, FRIDAY, simulated=True)
            session.add(OpSession(user_id=uid, sleeve="O3B", trade_date=FRIDAY, mode="PAPER",
                                  state="SKIPPED", skip_reasons=["GAP_TOO_SMALL"]))  # fmt: skip
            last_week = FRIDAY - dt.timedelta(days=9)
            session.add(OpSession(user_id=uid, sleeve="O3B", trade_date=last_week, mode="PAPER",
                                  state="SKIPPED", skip_reasons=["OLD"]))  # fmt: skip
            await session.flush()
            rows = await week_rows(session, uid, FRIDAY)
            alert = weekly_alert(rows, FRIDAY)
        assert "O2 paper PAPER_ONE_LOT: 1 traded" in alert.summary
        assert (
            "O3B paper PAPER_ONE_LOT: 0 traded, 1 skipped" in alert.summary
        )  # last week's excluded


@requires_db
class TestTheFirstLiveMultiplier:
    @pytest.mark.parametrize(("real", "half"), [(4, True), (5, False)])
    async def test_five_real_rows_lift_half_size(self, op_url: str, real: int, half: bool) -> None:  # noqa: F811
        async with _rolled_back(op_url) as session:
            uid = await _user(session)
            session.add(OpSleeveConfig(user_id=uid, sleeve="O2", sleeve_capital_inr=Decimal(500000),
                                       risk_per_trade_pct=Decimal("0.5"), max_lots=4))  # fmt: skip
            await session.flush()
            for i in range(real):
                await _journal(session, uid, FRIDAY - dt.timedelta(days=i + 1), simulated=False)
            await _journal(session, uid, FRIDAY, simulated=True)  # a paper row never counts
            context = await load_context(session, uid, FRIDAY, lambda _s: Mode.LIVE)
        book = context.of(Sleeve.O2).book
        assert book.real_journal_rows == real
        cfg = OptionsConfig()
        sizing = size_for(
            book, default_risk_pct=cfg.directional.risk_per_trade_pct,
            default_max_lots=cfg.directional.max_lots, risk_per_lot_inr=Decimal(600), lot_size=65,
            config=cfg.sizing, ceilings=OptionsCeilings(),
        )  # fmt: skip
        assert sizing.half_size is half
