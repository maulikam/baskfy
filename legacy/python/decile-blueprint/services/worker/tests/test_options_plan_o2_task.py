"""OP7 — the O2 plan builder on a database, its task, its alert.

``docs/options/06`` OP7 AC asserted here (the arithmetic is ``packages/core/tests/
test_options_plan_o2.py``'s):

* the fixture morning (Mon 19 Oct 2026, the 10:00-10:04 break) writes **one** ``op_session``
  (``PLANNED``), one ``op_plan`` (``ISSUED``, ``LONG_OPTION``, ``expires_at`` 10:35:45) and **one**
  ``op_leg`` — Tuesday's 25,000 CE — to the rupee;
* **idempotent per date** — a second call returns the same ``plan_id`` and sends no second alert;
* a day whose entry window closes with no with-trend break writes ``SKIPPED / NO_TRIGGER``;
* the plan lapses at ``expires_at`` (the shared ``lapse_expired``);
* ``AlertName.OPTIONS_PLAN`` renders through the mail transport with the contract, the stop, the
  target, the time stop and the gap-through worst case;
* the task is dark unless the monitor **and** collect flags are on, and it reaches **no provider
  at all** — a long option's ceiling is the premium cap, not a broker's margin (OP7.3).
"""

from __future__ import annotations

import datetime as dt
import inspect
import sys
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from test_options_scan_task import (
    IST,
    _index_def,
    _index_instrument,
    _paper,
    _rolled_back,
    _snapshot_rows,
    op_url,  # noqa: F401 - the module-scoped migration fixture
    requires_db,
)

from baskfy_api.email import Mailer, Message
from baskfy_api.settings import Settings
from baskfy_core.models import (
    AppUser,
    IndexSnapshotDaily,
    OpChainSnapshot,
    OpContract,
    OpIndexMinute,
    OpLeg,
    OpPlan,
    OpSession,
)
from baskfy_core.options.bars import Bar
from baskfy_core.options.scan import Snapshot
from baskfy_worker.alerts import AlertName, dispatch
from baskfy_worker.options import plan_o2 as P
from baskfy_worker.options.plan_o2 import build_o2_plan, plan_o2_gate_free, plan_o2_minute

CORE_TESTS: Final = Path(__file__).resolve().parents[3] / "packages" / "core" / "tests"
if str(CORE_TESTS) not in sys.path:
    sys.path.insert(0, str(CORE_TESTS))

import options_scan_fixtures as F  # noqa: E402 - after the sys.path insert above

DAY = F.O2_UP_BREAK  # Mon 19 Oct 2026
NOW = dt.datetime(2026, 10, 19, 10, 5, 45, tzinfo=IST)
INSIDE = "2026-10-19T10:05:45+05:30"


class Recording:
    """The transport Mailpit would be: every message the real ``Mailer`` hands it, kept."""

    def __init__(self) -> None:
        self.sent: list[Message] = []

    async def send(self, message: Message) -> None:
        self.sent.append(message)


# --- the free gate -------------------------------------------------------------------------------


class TestTheGate:
    def test_the_monitor_flag_refuses_first(self) -> None:
        out = plan_o2_gate_free(NOW, monitor_enabled=False, collect_enabled=True, user_id=1)
        assert out == "BASKFY_OPTIONS_MONITOR_ENABLED is false"

    def test_the_collector_flag_refuses_next(self) -> None:
        out = plan_o2_gate_free(NOW, monitor_enabled=True, collect_enabled=False, user_id=1)
        assert out is not None and out.startswith("BASKFY_OPTIONS_COLLECT_ENABLED is false")

    @pytest.mark.parametrize(
        ("clock", "refused"),
        [((9, 29), True), ((9, 30), False), ((13, 34), False), ((13, 35), True)],
    )
    def test_the_window_is_o2s_entry_window_plus_the_no_trigger_grace(
        self, clock: tuple[int, int], refused: bool
    ) -> None:
        now = dt.datetime(2026, 10, 19, *clock, tzinfo=IST)
        out = plan_o2_gate_free(now, monitor_enabled=True, collect_enabled=True, user_id=1)
        assert (out is not None) is refused

    def test_no_tenant(self) -> None:
        out = plan_o2_gate_free(NOW, monitor_enabled=True, collect_enabled=True, user_id=None)
        assert out == "no BASKFY_SOLE_USER_ID configured"

    def test_the_task_refuses_before_any_database_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from baskfy_worker.settings import WorkerSettings  # noqa: PLC0415
        from baskfy_worker.tasks import celery_tasks  # noqa: PLC0415

        def no_db(operation: object) -> object:
            raise AssertionError(f"a database session was opened for {operation!r}")

        monkeypatch.setattr(celery_tasks, "run_in_session", no_db)
        monkeypatch.setattr(celery_tasks, "sole_user_id", lambda: 1)
        monkeypatch.setattr(
            celery_tasks,
            "get_worker_settings",
            lambda: WorkerSettings(_env_file=None, options_collect_enabled=True),
        )
        out = celery_tasks.options_plan_o2_task.run(INSIDE)
        assert out["skipped"] == "BASKFY_OPTIONS_MONITOR_ENABLED is false"

    def test_beat_runs_it_each_minute_of_the_entry_window(self) -> None:
        from baskfy_worker.celery_app import BEAT_SCHEDULE, QUEUE_DEFAULT  # noqa: PLC0415

        entry = BEAT_SCHEDULE["options-plan-o2"]
        assert entry["task"] == "baskfy.options.plan_o2"
        options = entry["options"]
        assert isinstance(options, dict) and options["queue"] == QUEUE_DEFAULT
        assert options["countdown"] == 45 and options["expires"] == 55

    def test_no_order_path_and_no_provider_in_the_builder(self) -> None:
        """OP7.3: O2 asks no broker anything — not even the margin calculator."""
        body = inspect.getsource(P)
        for name in (
            "place_order",
            "baskfy_execution",
            "OrderGateway",
            "place_gtt",
            ".place(",
            "baskfy_providers",
            "build_options_kite",
            "basket_order_margins",
        ):
            assert name not in body, name

    def test_the_task_builds_no_kite_client(self) -> None:
        from baskfy_worker.tasks import celery_tasks  # noqa: PLC0415

        body = inspect.getsource(celery_tasks.options_plan_o2_task)
        assert "build_options_kite" not in body and "kite_margin_reader" not in body


# --- on a real database --------------------------------------------------------------------------


def _counter_bars() -> tuple[Bar, ...]:
    """The fixture Monday's opening range, then a tape that falls: every break is counter-trend
    against the up EMA, so the day never triggers (``04`` §4.2)."""
    before = F.zigzag(Decimal("25020"), Decimal("10"), F.to_minutes(10, 0))
    after = F.ramp(before[-1], Decimal("-4"), F.to_minutes(15, 30) - len(before))
    return F.bars_from_closes(DAY, Decimal("25020"), before + after)


async def _seed_o2_day(
    session: AsyncSession,
    bars: tuple[Bar, ...] | None = None,
    until: dt.datetime | None = None,
) -> int:
    """The fixture Monday as OP3 stores it: the master, the day's bars, the daily levels and the
    10:05 chain (the trigger minute's)."""
    user = AppUser(
        public_id=f"op7-{uuid.uuid4().hex[:8]}", email=f"op7-{uuid.uuid4().hex[:8]}@x.test"
    )
    session.add(user)
    await session.flush()
    await session.execute(
        sa.insert(OpContract),
        [
            {
                "instrument_token": c.instrument_token,
                "tradingsymbol": c.tradingsymbol,
                "underlying": "NIFTY",
                "expiry": c.expiry,
                "strike": c.strike,
                "option_type": c.option_type.value,
                "lot_size": c.lot_size,
                "tick_size": c.tick_size,
                "first_seen": DAY,
                "last_seen": DAY,
                "expired": False,
            }
            for c in F.MASTER
        ],
    )
    day_bars = bars if bars is not None else F.o2_up_break_bars()
    nifty = await _index_instrument(session)
    await session.execute(
        sa.insert(OpIndexMinute),
        [
            {
                "instrument_id": nifty,
                "ts": b.ts,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "source": "KITE_HIST",
            }
            for b in day_bars
            if b.ts < (until or F.at(DAY, 10, 10))
        ],
    )
    n50 = await _index_def(session, "nifty-50", 32001)
    vix = await _index_def(session, "india-vix", 32002)
    closes = F.rising_closes()
    await session.execute(
        sa.insert(IndexSnapshotDaily),
        [
            {"index_id": n50, "date": DAY - dt.timedelta(days=len(closes) - i), "level": level}
            for i, level in enumerate(closes)
        ]
        + [{"index_id": vix, "date": DAY - dt.timedelta(days=1), "level": Decimal(14)}],
    )
    await session.execute(sa.insert(OpChainSnapshot), _snapshot_rows(_chain_at(day_bars)))
    await session.flush()
    return int(user.id)


def _chain_at(bars: tuple[Bar, ...], minute: dt.datetime | None = None) -> Snapshot:
    at_minute = minute or F.at(DAY, 10, 5)
    spot = [b for b in bars if b.ts + dt.timedelta(minutes=1) <= at_minute][-1].close
    exps = [dt.date(2026, 10, 20), dt.date(2026, 10, 27)]
    fwd = {e: F.carry_forward(spot, at_minute, e) for e in exps}
    return F.snapshot(at_minute, spot, exps, F.skew(0.30, 0.33), forwards=fwd)


async def _sessions(session: AsyncSession, user_id: int) -> list[OpSession]:
    return list(
        (
            await session.execute(
                sa.select(OpSession).where(OpSession.user_id == user_id, OpSession.sleeve == "O2")
            )
        )
        .scalars()
        .all()
    )


@requires_db
class TestTheBuilderOnADatabase:
    async def test_the_fixture_morning_writes_one_plan_to_the_rupee(self, op_url: str) -> None:  # noqa: F811
        async with _rolled_back(op_url) as session:
            user_id = await _seed_o2_day(session)
            result = await build_o2_plan(session, user_id, NOW, trading_day=True, mode_of=_paper)
            assert result.report.state == "PLANNED" and result.report.created
            (row,) = await _sessions(session, user_id)
            assert (row.state, row.mode, row.verdict, row.expiry_used) == (
                "PLANNED",
                "PAPER",
                "TRADE",
                dt.date(2026, 10, 20),
            )
            plan = (
                await session.execute(sa.select(OpPlan).where(OpPlan.session_id == row.id))
            ).scalar_one()
            assert plan.plan_id == row.plan_id == result.report.plan_id
            assert (plan.status, plan.kind, plan.structure, plan.sizing_mode) == (
                "ISSUED",
                "ENTRY",
                "LONG_OPTION",
                "PAPER_ONE_LOT",
            )
            assert plan.expires_at == NOW + dt.timedelta(minutes=30)
            assert (plan.credit_points, plan.width_points, plan.margin_required_inr) == (
                None,
                None,
                None,
            )
            assert (plan.debit_points, plan.lots, plan.lot_size) == (Decimal("203.75"), 1, 65)
            assert plan.max_loss_inr == Decimal("3973.13")
            assert plan.risk_per_lot_inr == Decimal("4273.13")
            assert plan.profit_target_inr == Decimal("7946.25")
            assert plan.expected_cost_inr == Decimal("78.34")
            assert plan.cost_share == Decimal("0.0099")
            assert plan.detail is not None
            costs, exits = plan.detail["costs"], plan.detail["exits"]
            assert isinstance(costs, dict) and costs["stt"] == "19.66" and costs["orders"] == 2
            assert isinstance(exits, dict)
            assert exits["stop_price"] == "142.63" and exits["target_price"] == "326.00"
            assert plan.detail["gap_through_inr"] == "13243.75"
            legs = (
                (
                    await session.execute(
                        sa.select(OpLeg).where(OpLeg.plan_id == plan.id).order_by(OpLeg.seq)
                    )
                )
                .scalars()
                .all()
            )
            assert [(lg.role, lg.side, lg.tradingsymbol, lg.limit_price) for lg in legs] == [
                ("LONG_CALL", "BUY", "NIFTY26102025000CE", Decimal("203.75")),
            ]
            (leg,) = legs
            assert leg.quantity == 65 and leg.simulated and leg.status == "PENDING"
            assert result.alert is not None and result.alert.name is AlertName.OPTIONS_PLAN

    async def test_idempotent_per_date(self, op_url: str) -> None:  # noqa: F811
        async with _rolled_back(op_url) as session:
            user_id = await _seed_o2_day(session)
            first = await build_o2_plan(session, user_id, NOW, trading_day=True, mode_of=_paper)
            again = await build_o2_plan(
                session,
                user_id,
                NOW + dt.timedelta(minutes=2),
                trading_day=True,
                mode_of=_paper,
            )
            assert again.report.plan_id == first.report.plan_id
            assert again.report.created is False and again.alert is None
            assert len(await _sessions(session, user_id)) == 1
            count = (
                await session.execute(
                    sa.select(sa.func.count()).select_from(OpPlan).where(OpPlan.user_id == user_id)
                )
            ).scalar_one()
            assert count == 1

    async def test_a_day_that_never_triggers_is_a_skipped_session(self, op_url: str) -> None:  # noqa: F811
        """The counter-trend tape: every break is against the trend, so nothing is traded."""
        async with _rolled_back(op_url) as session:
            user_id = await _seed_o2_day(session, bars=_counter_bars(), until=F.at(DAY, 13, 35))
            waiting = await build_o2_plan(session, user_id, NOW, trading_day=True, mode_of=_paper)
            assert waiting.report.state == "NOT_READY" and await _sessions(session, user_id) == []
            after = dt.datetime(2026, 10, 19, 13, 33, tzinfo=IST)
            done = await build_o2_plan(session, user_id, after, trading_day=True, mode_of=_paper)
            (row,) = await _sessions(session, user_id)
            assert (row.state, list(row.skip_reasons), row.plan_id) == (
                "SKIPPED",
                ["NO_TRIGGER"],
                None,
            )
            assert done.alert is not None and "NO_TRIGGER" in done.alert.summary

    async def test_the_minute_lapses_an_expired_plan(self, op_url: str) -> None:  # noqa: F811
        async with _rolled_back(op_url) as session:
            user_id = await _seed_o2_day(session)
            report, alerts = await plan_o2_minute(
                session, user_id, NOW, trading_day=True, mode_of=_paper
            )
            assert len(alerts) == 1 and report["lapsed"] == []
            after = NOW + dt.timedelta(minutes=31)
            report, alerts = await plan_o2_minute(
                session, user_id, after, trading_day=True, mode_of=_paper
            )
            lapsed = report["lapsed"]
            assert alerts == [] and isinstance(lapsed, list) and len(lapsed) == 1
            (row,) = await _sessions(session, user_id)
            assert row.state == "LAPSED"
            status = (
                await session.execute(sa.select(OpPlan.status).where(OpPlan.session_id == row.id))
            ).scalar_one()
            assert status == "LAPSED"

    async def test_the_alert_renders_through_the_mail_transport(self, op_url: str) -> None:  # noqa: F811
        async with _rolled_back(op_url) as session:
            user_id = await _seed_o2_day(session)
            result = await build_o2_plan(session, user_id, NOW, trading_day=True, mode_of=_paper)
        assert result.alert is not None
        sink = Recording()
        settings = Settings(ops_alert_email="ops@x.test", email_transport="console")
        sent = await dispatch(result.alert, settings, mailer=Mailer(sink))
        assert "email" in str(sent["delivered_to"])
        (message,) = sink.sent
        for text in (
            "O2 2026-10-19 PAPER plan O2-20261019-",
            "BUY 65 NIFTY26102025000CE @ 203.75",
            "1 lot(s) of 65",
            "UP break of 25044.52 at 10:04",
            "premium ₹13243.75",
            "stop 142.63",
            "target 326.00",
            "time stop 45 min",
            "hard exit 15:00",
            "gap-through worst case ₹13243.75",
            "round-trip costs ₹78.34",
            "Expires 10:35",
            "docs/runbooks/11-options-plan.md",
        ):
            assert text in message.text, text
        assert "OPTIONS_PLAN" in message.text
