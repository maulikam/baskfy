"""OP14 — the options book's health on a database: the gauges' facts and the in-process checks.

* ``baskfy_api.options_health.read_options_health`` reads each fact the ``baskfy-options`` rules
  watch from real rows (`TestTheFacts`); the rules themselves fire from synthetic series in
  ``services/api/tests/test_options_alerts.py``;
* each check (09:20, 10:20, hard exit + 3 min, 15:35) fails on the fixture that should fail it,
  passes on the one that should not, and skips with its switch off (`TestTheChecks`);
* the task runs a check only at its minute, and Beat carries it (`TestTheTask`).
"""

from __future__ import annotations

import datetime as dt
import sys
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from test_options_scan_task import (
    _rolled_back,
    op_url,  # noqa: F401 - the module-scoped migration fixture
    requires_db,
)

from baskfy_api.options_health import read_options_health
from baskfy_core.models import (
    AppUser,
    OpChainSnapshot,
    OpContract,
    OpPlan,
    OpPosition,
    OpScan,
    OpSession,
)
from baskfy_core.options.config import OptionsConfig, Sleeve
from baskfy_worker.alerts import AlertName, Severity
from baskfy_worker.options.checks import (
    Switches,
    check_alert,
    due_checks,
    hard_exit_checks,
    run_checks,
)

CORE_TESTS: Final = Path(__file__).resolve().parents[3] / "packages" / "core" / "tests"
if str(CORE_TESTS) not in sys.path:
    sys.path.insert(0, str(CORE_TESTS))

import options_scan_fixtures as F  # noqa: E402 - after the sys.path insert above

DAY = F.QUIET_MONTHLY  # Tue 27 Oct 2026, the monthly expiry: an O1-M day
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
ON = Switches(scan_enabled=True, collect_enabled=True, monitor_enabled=True)
OFF = Switches(scan_enabled=False, collect_enabled=False, monitor_enabled=False)
CFG = OptionsConfig()


def at(hh: int, mm: int) -> dt.datetime:
    return dt.datetime.combine(DAY, dt.time(hh, mm), IST)


async def _user(session: AsyncSession) -> int:
    user = AppUser(
        public_id=f"op14-{uuid.uuid4().hex[:8]}", email=f"op14-{uuid.uuid4().hex[:8]}@x.test"
    )
    session.add(user)
    await session.flush()
    return int(user.id)


async def _open_position(session: AsyncSession, uid: int, sleeve: str = "O1M") -> None:
    plan_id = f"{sleeve}-op14-{uuid.uuid4().hex[:8]}"
    row = OpSession(user_id=uid, sleeve=sleeve, trade_date=DAY, mode="PAPER", state="OPEN",
                    plan_id=plan_id)  # fmt: skip
    session.add(row)
    await session.flush()
    session.add(OpPlan(user_id=uid, plan_id=plan_id, session_id=row.id, sleeve=sleeve,
                       structure="IRON_CONDOR", kind="ENTRY", sizing_mode="PAPER_ONE_LOT",
                       issued_at=at(10, 0), expires_at=at(10, 15), lots=1, lot_size=65,
                       risk_budget_inr=Decimal(2500), status="CONFIRMED"))  # fmt: skip
    session.add(OpPosition(session_id=row.id, user_id=uid, leg_ids=[], entry_points=Decimal(30),
                           entry_inr=Decimal(1950), lots=1, opened_at=at(10, 1),
                           hard_exit_at=at(14, 30), simulated=True))  # fmt: skip
    await session.flush()


async def _chain_minutes(session: AsyncSession, first: dt.datetime, count: int) -> None:
    token = F.MASTER[0].instrument_token
    await session.execute(
        sa.insert(OpChainSnapshot),
        [
            {"ts": first + dt.timedelta(minutes=i), "instrument_token": token, "expiry": DAY,
             "strike": F.MASTER[0].strike, "option_type": F.MASTER[0].option_type.value,
             "spot": Decimal(25000), "bid": Decimal(10), "ask": Decimal("10.20"),
             "depth_json": {}, "volume": 0, "oi": 0, "source": "QUOTE"}
            for i in range(count)
        ],
    )  # fmt: skip


async def _scan(session: AsyncSession, uid: int, sleeves: list[Sleeve], ts: dt.datetime) -> None:
    for sleeve in sleeves:
        session.add(
            OpScan(user_id=uid, sleeve=sleeve.value, trade_date=DAY, ts=ts, state="WAITING")
        )
    await session.flush()


@requires_db
class TestTheFacts:
    async def test_open_after_hard_exit_counts_only_past_the_grace(self, op_url: str) -> None:  # noqa: F811
        async with _rolled_back(op_url) as session:
            await _open_position(session, await _user(session))
            before = await read_options_health(session, trading_day=True, now=at(14, 31))
            after = await read_options_health(session, trading_day=True, now=at(14, 32))
            assert (before.open_after_hard_exit, after.open_after_hard_exit) == (0, 1)
            assert after.sessions_today >= 1

    async def test_the_collector_gap_and_the_limiter_share(self, op_url: str) -> None:  # noqa: F811
        async with _rolled_back(op_url) as session:
            await _chain_minutes(session, at(10, 0), 10)  # 10:00-10:09, one call a minute
            health = await read_options_health(session, trading_day=True, now=at(10, 15))
            assert health.collector_gap_minutes == 6  # since 10:09
            # (10:00, 10:10]: nine calls in ten minutes, 0.9 a minute of the quote family's 60:
            # 1.5 %, rounded up.
            share = await read_options_health(session, trading_day=True, now=at(10, 10))
            assert share.limiter_share_pct == 2
            quiet = await read_options_health(session, trading_day=True, now=at(16, 0))
            assert quiet.collector_gap_minutes == 0  # outside the session

    async def test_the_scan_goes_stale_and_a_holiday_is_quiet(self, op_url: str) -> None:  # noqa: F811
        async with _rolled_back(op_url) as session:
            await _scan(session, await _user(session), [Sleeve.O2], at(11, 0))
            stale = await read_options_health(session, trading_day=True, now=at(11, 7))
            assert stale.scan_stale_minutes == 7
            holiday = await read_options_health(session, trading_day=False, now=at(11, 7))
            assert holiday.scan_stale_minutes == 0 and holiday.collector_gap_minutes == 0


@requires_db
class TestTheChecks:
    async def test_flat_after_hard_exit_fails_on_an_open_position(self, op_url: str) -> None:  # noqa: F811
        async with _rolled_back(op_url) as session:
            await _open_position(session, await _user(session))
            (result,) = await run_checks(session, ("FLAT_AFTER_HARD_EXIT",), at(14, 33), OFF)
            assert not result.ok and "O1M" in result.detail
            alert = check_alert(result, at(14, 33))
            assert alert.name is AlertName.OPTIONS_CHECK_FAILED
            assert alert.severity is Severity.CRITICAL
            assert alert.labels == {"check": "FLAT_AFTER_HARD_EXIT"}

    async def test_the_collector_needs_360_minutes(self, op_url: str) -> None:  # noqa: F811
        async with _rolled_back(op_url) as session:
            await _chain_minutes(session, at(9, 15), 359)
            (short,) = await run_checks(session, ("COLLECTOR_FULL_DAY",), at(15, 35), ON)
            assert not short.ok and "359" in short.detail
            await _chain_minutes(session, at(15, 14), 1)
            (full,) = await run_checks(session, ("COLLECTOR_FULL_DAY",), at(15, 35), ON)
            assert full.ok
            (off,) = await run_checks(session, ("COLLECTOR_FULL_DAY",), at(15, 35), OFF)
            assert off.skipped and off.ok

    async def test_every_sleeve_scanned_by_0920(self, op_url: str) -> None:  # noqa: F811
        async with _rolled_back(op_url) as session:
            uid = await _user(session)
            await _scan(session, uid, [Sleeve.O1M, Sleeve.O2], at(9, 16))
            (partial,) = await run_checks(session, ("SCAN_STARTED",), at(9, 20), ON)
            assert not partial.ok and "O3B" in partial.detail
            await _scan(session, uid, [Sleeve.O1W, Sleeve.O3A, Sleeve.O3B], at(9, 17))
            (whole,) = await run_checks(session, ("SCAN_STARTED",), at(9, 20), ON)
            assert whole.ok

    async def test_an_o1_day_needs_its_verdict_by_1020(self, op_url: str) -> None:  # noqa: F811
        async with _rolled_back(op_url) as session:
            await session.execute(
                sa.insert(OpContract),
                [
                    {"instrument_token": c.instrument_token, "tradingsymbol": c.tradingsymbol,
                     "underlying": "NIFTY", "expiry": c.expiry, "strike": c.strike,
                     "option_type": c.option_type.value, "lot_size": c.lot_size,
                     "tick_size": c.tick_size, "first_seen": DAY, "last_seen": DAY,
                     "expired": False}
                    for c in F.MASTER
                ],
            )  # fmt: skip
            (missing,) = await run_checks(session, ("O1_DECIDED",), at(10, 20), ON)
            assert not missing.ok and "O1M" in missing.detail
            uid = await _user(session)
            session.add(OpSession(user_id=uid, sleeve="O1M", trade_date=DAY, mode="PAPER",
                                  state="SKIPPED", skip_reasons=["GAP_TOO_BIG"]))  # fmt: skip
            await session.flush()
            (decided,) = await run_checks(session, ("O1_DECIDED",), at(10, 20), ON)
            assert decided.ok
            (dark,) = await run_checks(session, ("O1_DECIDED",), at(10, 20), OFF)
            assert dark.skipped


class TestTheTask:
    def test_each_check_is_due_at_its_minute_and_only_then(self) -> None:
        assert due_checks(at(9, 20), CFG) == ("SCAN_STARTED",)
        assert due_checks(at(10, 20), CFG) == ("O1_DECIDED",)
        assert hard_exit_checks(CFG) == (dt.time(14, 33), dt.time(14, 48), dt.time(15, 3))
        for hh, mm in ((14, 33), (14, 48), (15, 3)):
            assert due_checks(at(hh, mm), CFG) == ("FLAT_AFTER_HARD_EXIT",)
        assert due_checks(at(15, 35), CFG) == ("COLLECTOR_FULL_DAY",)
        assert due_checks(at(11, 20), CFG) == ()

    def test_beat_covers_every_due_minute(self) -> None:
        from celery.schedules import crontab  # noqa: PLC0415

        from baskfy_worker.celery_app import BEAT_SCHEDULE  # noqa: PLC0415

        schedule = BEAT_SCHEDULE["options-checks"]["schedule"]
        assert isinstance(schedule, crontab)
        assert BEAT_SCHEDULE["options-checks"]["task"] == "baskfy.options.checks"
        for hh, mm in ((9, 20), (10, 20), (14, 33), (14, 48), (15, 3), (15, 35)):
            assert hh in schedule.hour and mm in schedule.minute, (hh, mm)

    def test_no_session_is_opened_when_nothing_is_due(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from baskfy_worker.tasks import celery_tasks  # noqa: PLC0415

        def no_db(operation: object) -> object:
            raise AssertionError(f"a database session was opened for {operation!r}")

        monkeypatch.setattr(celery_tasks, "run_in_session", no_db)
        out = celery_tasks.options_checks_task.run("2026-10-27T11:20:00+05:30")
        assert out["skipped"] == "no options check is due at this minute"
