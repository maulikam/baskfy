"""OP6 — the O1 plan builder on a database, its task, its alert.

``docs/options/06`` OP6 AC asserted here (the arithmetic is ``packages/core/tests/
test_options_plan.py``'s):

* the fixture morning writes **one** ``op_session`` (``PLANNED``), one ``op_plan`` (``ISSUED``,
  ``expires_at`` 10:15) and four ``op_leg`` rows in never-naked send order, to the rupee;
* **idempotent per date** — a second call returns the same ``plan_id``, asks the margin
  calculator nothing and sends no second alert;
* O1-W on the monthly Tuesday → **no session**; a slot held by O3 → ``SKIPPED /
  REJECTED_SLOT_TAKEN``; the plan lapses at ``expires_at``;
* ``AlertName.OPTIONS_PLAN`` renders through the mail transport (a capturing ``Mailer`` in place of
  Mailpit) with the legs, credit, costs and margin;
* the task is dark unless the monitor **and** collect flags are on, and its module names no order
  path.
"""

from __future__ import annotations

import datetime as dt
import inspect
from collections.abc import Sequence
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from test_options_scan_task import (
    DAY,
    IST,
    _paper,
    _rolled_back,
    _seed_day,
    op_url,  # noqa: F401 - the module-scoped migration fixture
    requires_db,
)

from baskfy_api.email import Mailer, Message
from baskfy_api.settings import Settings
from baskfy_core.models import OpLeg, OpPlan, OpSession
from baskfy_core.options.config import Side, Sleeve
from baskfy_core.options.plan import MarginLeg
from baskfy_worker.alerts import AlertName, dispatch
from baskfy_worker.options import plan as P
from baskfy_worker.options.plan import build_o1_plan, plan_gate_free, plan_o1_minute

NOW = dt.datetime(2026, 10, 27, 10, 0, 40, tzinfo=IST)


class CountingMargin:
    """The broker's calculator, faked with the probe's figure; counts every basket asked."""

    def __init__(self) -> None:
        self.asked: list[tuple[MarginLeg, ...]] = []

    def __call__(self, legs: Sequence[MarginLeg]) -> Decimal | None:
        self.asked.append(tuple(legs))
        return Decimal("130903.05") if len(legs) == 4 else Decimal("98250.00")


# --- the free gate -------------------------------------------------------------------------------


class TestTheGate:
    def test_the_monitor_flag_refuses_first(self) -> None:
        out = plan_gate_free(NOW, monitor_enabled=False, collect_enabled=True, user_id=1)
        assert out == "BASKFY_OPTIONS_MONITOR_ENABLED is false"

    def test_the_collector_flag_refuses_next(self) -> None:
        out = plan_gate_free(NOW, monitor_enabled=True, collect_enabled=False, user_id=1)
        assert out is not None and out.startswith("BASKFY_OPTIONS_COLLECT_ENABLED is false")

    @pytest.mark.parametrize(
        ("clock", "refused"),
        [((9, 59), True), ((10, 0), False), ((10, 16), False), ((10, 17), True)],
    )
    def test_the_window(self, clock: tuple[int, int], refused: bool) -> None:
        now = dt.datetime(2026, 10, 27, *clock, tzinfo=IST)
        out = plan_gate_free(now, monitor_enabled=True, collect_enabled=True, user_id=1)
        assert (out is not None) is refused

    def test_no_tenant(self) -> None:
        out = plan_gate_free(NOW, monitor_enabled=True, collect_enabled=True, user_id=None)
        assert out == "no BASKFY_SOLE_USER_ID configured"

    def test_the_monitor_flag_defaults_off(self) -> None:
        from baskfy_worker.settings import WorkerSettings  # noqa: PLC0415

        assert WorkerSettings(_env_file=None).options_monitor_enabled is False

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
        out = celery_tasks.options_plan_o1_task.run("2026-10-27T10:00:40+05:30")
        assert out["skipped"] == "BASKFY_OPTIONS_MONITOR_ENABLED is false"

    def test_beat_runs_it_each_minute_1000_to_1016(self) -> None:
        from baskfy_worker.celery_app import BEAT_SCHEDULE, QUEUE_DEFAULT  # noqa: PLC0415

        entry = BEAT_SCHEDULE["options-plan-o1"]
        assert entry["task"] == "baskfy.options.plan_o1"
        options = entry["options"]
        assert isinstance(options, dict) and options["queue"] == QUEUE_DEFAULT
        assert options["countdown"] == 40 and options["expires"] == 55

    def test_no_order_path_in_the_builder(self) -> None:
        body = inspect.getsource(P)
        for name in ("place_order", "baskfy_execution", "OrderGateway", "place_gtt", ".place("):
            assert name not in body, name
        assert "consider_positions=False" in body


# --- the alert, rendered -------------------------------------------------------------------------


class Recording:
    """The transport Mailpit would be: every message the real ``Mailer`` hands it, kept."""

    def __init__(self) -> None:
        self.sent: list[Message] = []

    async def send(self, message: Message) -> None:
        self.sent.append(message)


# --- on a real database --------------------------------------------------------------------------


async def _one(session: AsyncSession, user_id: int, sleeve: str) -> list[OpSession]:
    return list(
        (
            await session.execute(
                sa.select(OpSession).where(OpSession.user_id == user_id, OpSession.sleeve == sleeve)
            )
        )
        .scalars()
        .all()
    )


@requires_db
class TestTheBuilderOnADatabase:
    async def test_the_fixture_morning_writes_one_plan_to_the_rupee(self, op_url: str) -> None:  # noqa: F811
        async with _rolled_back(op_url) as session:
            user_id = await _seed_day(session)
            margin = CountingMargin()
            result = await build_o1_plan(
                session, user_id, Sleeve.O1M, NOW, trading_day=True, margin=margin, mode_of=_paper
            )
            assert result.report.state == "PLANNED" and result.report.created
            (row,) = await _one(session, user_id, "O1M")
            assert (row.state, row.mode, row.verdict, row.expiry_used) == (
                "PLANNED",
                "PAPER",
                "TRADE",
                DAY,
            )
            plan = (
                await session.execute(sa.select(OpPlan).where(OpPlan.session_id == row.id))
            ).scalar_one()
            assert plan.plan_id == row.plan_id == result.report.plan_id
            assert (plan.status, plan.kind, plan.structure, plan.sizing_mode) == (
                "ISSUED",
                "ENTRY",
                "IRON_CONDOR",
                "PAPER_ONE_LOT",
            )
            assert plan.expires_at == dt.datetime(2026, 10, 27, 10, 15, tzinfo=IST)
            assert (plan.credit_points, plan.width_points, plan.lots, plan.lot_size) == (
                Decimal("39.65"),
                Decimal("150.00"),
                1,
                65,
            )
            assert plan.max_loss_inr == Decimal("7172.75")
            assert plan.risk_per_lot_inr == Decimal("8172.75")
            assert plan.expected_cost_inr == Decimal("198.33")
            assert plan.cost_share == Decimal("0.1539")
            assert plan.margin_required_inr == Decimal("130903.05")
            assert plan.detail is not None
            costs = plan.detail["costs"]
            assert isinstance(costs, dict) and costs["gst"] == "29.32" and costs["stt"] == "6.01"
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
                ("LONG_PUT", "BUY", "NIFTY26102724700PE", Decimal("5.45")),
                ("LONG_CALL", "BUY", "NIFTY26102725300CE", Decimal("5.85")),
                ("SHORT_PUT", "SELL", "NIFTY26102724850PE", Decimal("23.75")),
                ("SHORT_CALL", "SELL", "NIFTY26102725150CE", Decimal("27.00")),
            ]
            assert all(lg.quantity == 65 and lg.simulated and lg.status == "PENDING" for lg in legs)
            # Two baskets asked: all four legs, and the wings with the first short.
            assert [len(b) for b in margin.asked] == [4, 3]
            assert margin.asked[0][3].side is Side.SELL
            assert result.alert is not None and result.alert.name is AlertName.OPTIONS_PLAN

    async def test_idempotent_per_date(self, op_url: str) -> None:  # noqa: F811
        async with _rolled_back(op_url) as session:
            user_id = await _seed_day(session)
            margin = CountingMargin()
            first = await build_o1_plan(
                session, user_id, Sleeve.O1M, NOW, trading_day=True, margin=margin, mode_of=_paper
            )
            later = NOW + dt.timedelta(minutes=3)
            again = await build_o1_plan(
                session, user_id, Sleeve.O1M, later, trading_day=True, margin=margin, mode_of=_paper
            )
            assert again.report.plan_id == first.report.plan_id
            assert again.report.created is False and again.alert is None
            assert len(margin.asked) == 2  # nothing asked the second time
            assert len(await _one(session, user_id, "O1M")) == 1
            count = (
                await session.execute(
                    sa.select(sa.func.count()).select_from(OpPlan).where(OpPlan.user_id == user_id)
                )
            ).scalar_one()
            assert count == 1

    async def test_o1w_on_the_monthly_writes_nothing(self, op_url: str) -> None:  # noqa: F811
        async with _rolled_back(op_url) as session:
            user_id = await _seed_day(session)
            result = await build_o1_plan(
                session, user_id, Sleeve.O1W, NOW, trading_day=True, margin=None, mode_of=_paper
            )
            assert result.report.state == "NO_SESSION" and result.alert is None
            assert await _one(session, user_id, "O1W") == []

    async def test_a_slot_held_by_o3_skips_with_the_code(self, op_url: str) -> None:  # noqa: F811
        async with _rolled_back(op_url) as session:
            user_id = await _seed_day(session)
            session.add(
                OpSession(
                    user_id=user_id,
                    sleeve="O3B",
                    trade_date=DAY,
                    mode="PAPER",
                    state="CONFIRMED",
                    slot_holder="O3B",
                )
            )
            await session.flush()
            margin = CountingMargin()
            result = await build_o1_plan(
                session, user_id, Sleeve.O1M, NOW, trading_day=True, margin=margin, mode_of=_paper
            )
            (row,) = await _one(session, user_id, "O1M")
            assert (row.state, list(row.skip_reasons), row.plan_id) == (
                "SKIPPED",
                ["REJECTED_SLOT_TAKEN"],
                None,
            )
            assert margin.asked == [] and result.alert is not None
            assert "REJECTED_SLOT_TAKEN" in result.alert.summary

    async def test_the_minute_lapses_an_expired_plan(self, op_url: str) -> None:  # noqa: F811
        async with _rolled_back(op_url) as session:
            user_id = await _seed_day(session)
            report, alerts = await plan_o1_minute(
                session, user_id, NOW, trading_day=True, margin=CountingMargin(), mode_of=_paper
            )
            assert len(alerts) == 1 and report["lapsed"] == []
            after = dt.datetime(2026, 10, 27, 10, 15, 40, tzinfo=IST)
            report, alerts = await plan_o1_minute(
                session, user_id, after, trading_day=True, margin=None, mode_of=_paper
            )
            lapsed = report["lapsed"]
            assert alerts == [] and isinstance(lapsed, list) and len(lapsed) == 1
            (row,) = await _one(session, user_id, "O1M")
            assert row.state == "LAPSED"
            status = (
                await session.execute(sa.select(OpPlan.status).where(OpPlan.session_id == row.id))
            ).scalar_one()
            assert status == "LAPSED"

    async def test_the_alert_renders_through_the_mail_transport(self, op_url: str) -> None:  # noqa: F811
        async with _rolled_back(op_url) as session:
            user_id = await _seed_day(session)
            result = await build_o1_plan(
                session,
                user_id,
                Sleeve.O1M,
                NOW,
                trading_day=True,
                margin=CountingMargin(),
                mode_of=_paper,
            )
        assert result.alert is not None
        sink = Recording()
        settings = Settings(ops_alert_email="ops@x.test", email_transport="console")
        sent = await dispatch(result.alert, settings, mailer=Mailer(sink))
        assert "email" in str(sent["delivered_to"])
        (message,) = sink.sent
        for text in (
            "O1-M 2026-10-27 PAPER plan O1M-20261027-",
            "credit 39.65 pts (₹2577.25)",
            "1 lot(s) of 65",
            "max loss ₹7172.75",
            "round-trip costs ₹198.33",
            "margin ₹130903.05",
            "1. BUY 65 NIFTY26102724700PE @ 5.45",
            "4. SELL 65 NIFTY26102725150CE @ 27.00",
            "Expires 10:15",
            "docs/runbooks/11-options-plan.md",
        ):
            assert text in message.text, text
        assert "OPTIONS_PLAN" in message.text
