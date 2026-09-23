"""OP8 — the O3 plan builder on a database, its task, its alert.

``docs/options/06`` OP8 AC asserted here (the arithmetic is ``packages/core/tests/
test_options_plan_o3.py``'s):

* the gap-hold expiry (Tue 13 Oct 2026) writes **one** O3-B ``op_session`` (``PLANNED``), one
  ``op_plan`` (``ISSUED``, ``DEBIT_SPREAD``, ``expires_at`` 10:00) and **two** ``op_leg`` rows —
  the 25,200 CE bought first, the 25,300 CE sold second — to the rupee, with the broker's margin
  for the hedged basket on the plan;
* **one O3 a day**: on a day both setups fire, O3-A reads O3-B's session and is
  ``SKIPPED / REJECTED_SLOT_TAKEN, O3B_HOLDS``;
* **idempotent per date** — a second call returns the same ``plan_id`` and sends no second alert;
* the plan lapses at ``expires_at`` (the shared ``lapse_expired``);
* ``AlertName.OPTIONS_PLAN`` renders through the mail transport with both legs in send order;
* the task is dark unless the monitor **and** collect flags are on, and the builder reaches no
  order path; its only broker read is the margin calculator, handed in by the task.
"""

from __future__ import annotations

import datetime as dt
import inspect
import sys
import uuid
from collections.abc import Sequence
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
from baskfy_core.options.config import Sleeve
from baskfy_core.options.plan import MarginLeg
from baskfy_core.options.scan import Snapshot
from baskfy_worker.alerts import AlertName, dispatch
from baskfy_worker.options import plan_o3 as P
from baskfy_worker.options.plan_o3 import build_o3_plan, plan_o3_gate_free, plan_o3_minute

CORE_TESTS: Final = Path(__file__).resolve().parents[3] / "packages" / "core" / "tests"
if str(CORE_TESTS) not in sys.path:
    sys.path.insert(0, str(CORE_TESTS))

import options_scan_fixtures as F  # noqa: E402 - after the sys.path insert above

DAY = F.GAP_HOLD  # Tue 13 Oct 2026, a weekly expiry
NOW = dt.datetime(2026, 10, 13, 9, 46, 20, tzinfo=IST)
O3A_NOW = dt.datetime(2026, 10, 13, 10, 21, 20, tzinfo=IST)
INSIDE = "2026-10-13T09:46:20+05:30"
MARGIN = Decimal("24812.50")


class Recording:
    """The transport Mailpit would be: every message the real ``Mailer`` hands it, kept."""

    def __init__(self) -> None:
        self.sent: list[Message] = []

    async def send(self, message: Message) -> None:
        self.sent.append(message)


class Calculator:
    """The broker's margin calculator, recorded: every basket asked about, one fixed answer."""

    def __init__(self, answer: Decimal | None = MARGIN, fails: bool = False) -> None:
        self.answer = answer
        self.fails = fails
        self.asked: list[list[tuple[str, str, int]]] = []

    def __call__(self, legs: Sequence[MarginLeg]) -> Decimal | None:
        self.asked.append([(lg.tradingsymbol, lg.side.value, lg.quantity) for lg in legs])
        if self.fails:
            raise RuntimeError("calculator down")
        return self.answer


# --- the free gate -------------------------------------------------------------------------------


class TestTheGate:
    def test_the_monitor_flag_refuses_first(self) -> None:
        out = plan_o3_gate_free(NOW, monitor_enabled=False, collect_enabled=True, user_id=1)
        assert out == "BASKFY_OPTIONS_MONITOR_ENABLED is false"

    def test_the_collector_flag_refuses_next(self) -> None:
        out = plan_o3_gate_free(NOW, monitor_enabled=True, collect_enabled=False, user_id=1)
        assert out is not None and out.startswith("BASKFY_OPTIONS_COLLECT_ENABLED is false")

    @pytest.mark.parametrize(
        ("clock", "refused"),
        [((9, 44), True), ((9, 45), False), ((13, 34), False), ((13, 35), True)],
    )
    def test_the_window_is_o3bs_plan_time_to_o3as_entry_window_plus_the_grace(
        self, clock: tuple[int, int], refused: bool
    ) -> None:
        now = dt.datetime(2026, 10, 13, *clock, tzinfo=IST)
        out = plan_o3_gate_free(now, monitor_enabled=True, collect_enabled=True, user_id=1)
        assert (out is not None) is refused

    def test_no_tenant(self) -> None:
        out = plan_o3_gate_free(NOW, monitor_enabled=True, collect_enabled=True, user_id=None)
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
        out = celery_tasks.options_plan_o3_task.run(INSIDE)
        assert out["skipped"] == "BASKFY_OPTIONS_MONITOR_ENABLED is false"

    def test_beat_runs_it_each_minute_of_the_window(self) -> None:
        from baskfy_worker.celery_app import BEAT_SCHEDULE, QUEUE_DEFAULT  # noqa: PLC0415

        entry = BEAT_SCHEDULE["options-plan-o3"]
        assert entry["task"] == "baskfy.options.plan_o3"
        options = entry["options"]
        assert isinstance(options, dict) and options["queue"] == QUEUE_DEFAULT
        assert options["countdown"] == 45 and options["expires"] == 55

    def test_no_order_path_and_no_provider_in_the_builder(self) -> None:
        """The builder is handed a margin *calculator*; it builds no client and sends nothing."""
        body = inspect.getsource(P)
        for name in (
            "place_order",
            "baskfy_execution",
            "OrderGateway",
            "place_gtt",
            ".place(",
            "baskfy_providers",
            "build_options_kite",
        ):
            assert name not in body, name

    def test_o3b_decides_before_o3a(self) -> None:
        """``04`` §5.5 is read from O3-B's session, so O3-B must be written first each minute."""
        assert P.ORDER == (Sleeve.O3B, Sleeve.O3A)


# --- on a real database --------------------------------------------------------------------------


def _both_bars() -> tuple[Bar, ...]:
    """The day both setups fire (as ``test_options_plan_o3.gap_then_break_bars``): the gap holds,
    then a clean break of the 60-bar range from 10:15."""
    first = F.ramp(Decimal("25200"), Decimal("0.5"), F.to_minutes(10, 15))
    rest = F.ramp(first[-1], Decimal("6"), F.to_minutes(15, 30) - len(first))
    return F.bars_from_closes(DAY, Decimal("25200"), first + rest)


def _chain_at(bars: tuple[Bar, ...], minute: dt.datetime) -> Snapshot:
    spot = [b for b in bars if b.ts + dt.timedelta(minutes=1) <= minute][-1].close
    exps = sorted({c.expiry for c in F.MASTER if c.expiry >= DAY})[:2]
    fwd = {e: F.carry_forward(spot, minute, e) for e in exps}
    return F.snapshot(minute, spot, exps, F.flat(0.14), forwards=fwd)


async def _seed_expiry(
    session: AsyncSession,
    bars: tuple[Bar, ...] | None = None,
    until: dt.datetime | None = None,
    chains: Sequence[dt.datetime] = (),
) -> int:
    """The expiry as OP3 stores it: the master, the day's bars, the daily levels, the chains."""
    user = AppUser(
        public_id=f"op8-{uuid.uuid4().hex[:8]}", email=f"op8-{uuid.uuid4().hex[:8]}@x.test"
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
    day_bars = bars if bars is not None else F.gap_hold_bars()
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
            if b.ts < (until or F.at(DAY, 9, 46))
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
    for minute in chains or (F.at(DAY, 9, 45),):
        await session.execute(
            sa.insert(OpChainSnapshot), _snapshot_rows(_chain_at(day_bars, minute))
        )
    await session.flush()
    return int(user.id)


async def _sessions(session: AsyncSession, user_id: int, sleeve: str) -> list[OpSession]:
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
    async def test_the_gap_hold_expiry_writes_one_spread_to_the_rupee(self, op_url: str) -> None:  # noqa: F811
        calc = Calculator()
        async with _rolled_back(op_url) as session:
            user_id = await _seed_expiry(session)
            result = await build_o3_plan(
                session, user_id, Sleeve.O3B, NOW, trading_day=True, margin=calc, mode_of=_paper
            )
            assert result.report.state == "PLANNED" and result.report.created
            (row,) = await _sessions(session, user_id, "O3B")
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
                "DEBIT_SPREAD",
                "PAPER_ONE_LOT",
            )
            assert plan.expires_at == F.at(DAY, 10, 0)  # min(09:46:20 + 30, 10:00)
            assert (plan.debit_points, plan.width_points, plan.credit_points) == (
                Decimal("26.95"),
                Decimal("100.00"),
                None,
            )
            assert (plan.lots, plan.lot_size) == (1, 65)
            assert plan.max_loss_inr == Decimal("1751.75")
            assert plan.risk_per_lot_inr == Decimal("2251.75")
            assert plan.profit_target_inr == Decimal("3448.25")
            assert plan.stop_inr == Decimal("875.88")
            assert plan.margin_required_inr == MARGIN
            assert plan.detail is not None
            exits = plan.detail["exits"]
            assert isinstance(exits, dict)
            assert (exits["target_value"], exits["stop_value"], exits["invalidation_level"]) == (
                "80.00",
                "13.48",
                "25100.00",
            )
            legs = (
                (
                    await session.execute(
                        sa.select(OpLeg).where(OpLeg.plan_id == plan.id).order_by(OpLeg.seq)
                    )
                )
                .scalars()
                .all()
            )
            assert [(lg.seq, lg.role, lg.side, lg.tradingsymbol) for lg in legs] == [
                (1, "LONG_CALL", "BUY", "NIFTY26101325200CE"),
                (2, "SHORT_CALL", "SELL", "NIFTY26101325300CE"),
            ]
            assert all(lg.quantity == 65 and lg.simulated for lg in legs)
            # The calculator was asked once, about the hedged basket (OP8.4).
            assert calc.asked == [
                [("NIFTY26101325200CE", "BUY", 65), ("NIFTY26101325300CE", "SELL", 65)]
            ]
            assert result.alert is not None and result.alert.name is AlertName.OPTIONS_PLAN

    async def test_a_failing_calculator_is_a_warning_on_paper(self, op_url: str) -> None:  # noqa: F811
        async with _rolled_back(op_url) as session:
            user_id = await _seed_expiry(session)
            result = await build_o3_plan(
                session,
                user_id,
                Sleeve.O3B,
                NOW,
                trading_day=True,
                margin=Calculator(fails=True),
                mode_of=_paper,
            )
            assert result.report.state == "PLANNED"
            assert result.report.detail is not None
            assert str(result.report.detail["margin"]).startswith("margin calculator failed")
            (row,) = await _sessions(session, user_id, "O3B")
            plan = (
                await session.execute(sa.select(OpPlan).where(OpPlan.session_id == row.id))
            ).scalar_one()
            assert plan.margin_required_inr is None
            assert plan.detail is not None
            warnings = plan.detail["warnings"]
            assert isinstance(warnings, list) and "MARGIN_UNKNOWN" in warnings

    async def test_one_o3_a_day_on_the_database(self, op_url: str) -> None:  # noqa: F811
        """Both setups fire; the minute builds O3-B at 09:46, the desk confirms it, and O3-A at
        10:21 reads that row and stands down."""
        bars = _both_bars()
        async with _rolled_back(op_url) as session:
            user_id = await _seed_expiry(
                session,
                bars=bars,
                until=F.at(DAY, 10, 21),
                chains=(F.at(DAY, 9, 45), F.at(DAY, 10, 20)),
            )
            first, _ = await plan_o3_minute(
                session, user_id, NOW, trading_day=True, margin=None, mode_of=_paper
            )
            # The desk confirmed O3-B at 09:50 (OP10's route will do this): it holds the day.
            await session.execute(
                sa.update(OpSession)
                .where(OpSession.user_id == user_id, OpSession.sleeve == "O3B")
                .values(state="CONFIRMED")
            )
            await session.execute(
                sa.update(OpPlan).where(OpPlan.user_id == user_id).values(status="CONFIRMED")
            )
            second, alerts = await plan_o3_minute(
                session, user_id, O3A_NOW, trading_day=True, margin=None, mode_of=_paper
            )
            (o3b,) = await _sessions(session, user_id, "O3B")
            (o3a,) = await _sessions(session, user_id, "O3A")
            assert o3b.state == "CONFIRMED"
            assert (o3a.state, list(o3a.skip_reasons)) == (
                "SKIPPED",
                ["REJECTED_SLOT_TAKEN", "O3B_HOLDS"],
            )
            assert len(alerts) == 1 and "O3B_HOLDS" in alerts[0].summary
            assert isinstance(second["lapsed"], list) and second["lapsed"] == []
            assert first["sleeves"] is not None

    async def test_idempotent_per_date(self, op_url: str) -> None:  # noqa: F811
        async with _rolled_back(op_url) as session:
            user_id = await _seed_expiry(session)
            first = await build_o3_plan(
                session, user_id, Sleeve.O3B, NOW, trading_day=True, margin=None, mode_of=_paper
            )
            again = await build_o3_plan(
                session,
                user_id,
                Sleeve.O3B,
                NOW + dt.timedelta(minutes=2),
                trading_day=True,
                margin=None,
                mode_of=_paper,
            )
            assert again.report.plan_id == first.report.plan_id
            assert again.report.created is False and again.alert is None
            assert len(await _sessions(session, user_id, "O3B")) == 1
            count = (
                await session.execute(
                    sa.select(sa.func.count()).select_from(OpPlan).where(OpPlan.user_id == user_id)
                )
            ).scalar_one()
            assert count == 1

    async def test_the_minute_lapses_an_expired_plan(self, op_url: str) -> None:  # noqa: F811
        async with _rolled_back(op_url) as session:
            user_id = await _seed_expiry(session)
            report, alerts = await plan_o3_minute(
                session, user_id, NOW, trading_day=True, margin=None, mode_of=_paper
            )
            assert len(alerts) == 1 and report["lapsed"] == []
            after = F.at(DAY, 10, 0) + dt.timedelta(seconds=30)
            report, _ = await plan_o3_minute(
                session, user_id, after, trading_day=True, margin=None, mode_of=_paper
            )
            lapsed = report["lapsed"]
            assert isinstance(lapsed, list) and len(lapsed) == 1
            (row,) = await _sessions(session, user_id, "O3B")
            assert row.state == "LAPSED"

    async def test_a_non_expiry_day_writes_nothing(self, op_url: str) -> None:  # noqa: F811
        monday = dt.datetime(2026, 10, 12, 9, 46, 20, tzinfo=IST)
        async with _rolled_back(op_url) as session:
            user_id = await _seed_expiry(session)
            report, alerts = await plan_o3_minute(
                session, user_id, monday, trading_day=True, margin=None, mode_of=_paper
            )
            assert alerts == []
            sleeves = report["sleeves"]
            assert isinstance(sleeves, list)
            assert {s["state"] for s in sleeves if isinstance(s, dict)} == {"NO_SESSION"}
            assert await _sessions(session, user_id, "O3B") == []

    async def test_the_alert_renders_through_the_mail_transport(self, op_url: str) -> None:  # noqa: F811
        async with _rolled_back(op_url) as session:
            user_id = await _seed_expiry(session)
            result = await build_o3_plan(
                session,
                user_id,
                Sleeve.O3B,
                NOW,
                trading_day=True,
                margin=Calculator(),
                mode_of=_paper,
            )
        assert result.alert is not None
        sink = Recording()
        settings = Settings(ops_alert_email="ops@x.test", email_transport="console")
        sent = await dispatch(result.alert, settings, mailer=Mailer(sink))
        assert "email" in str(sent["delivered_to"])
        (message,) = sink.sent
        for text in (
            "O3B 2026-10-13 PAPER plan O3B-20261013-",
            "UP debit spread 25200/25300 CE",
            "BUY NIFTY26101325200CE first, then SELL NIFTY26101325300CE",
            "1 lot(s) of 65",
            "debit 26.95 of a 100-point width = max loss ₹1751.75",
            "target value 80.00 (₹3448.25)",
            "stop value 13.48 (₹875.88)",
            "invalidation at 25100",
            "hard exit 14:45",
            f"margin ₹{MARGIN}",
            "Expires 10:00",
            "docs/runbooks/11-options-plan.md",
        ):
            assert text in message.text, text
        assert "OPTIONS_PLAN" in message.text

    async def test_an_unconfirmed_o3b_lapses_and_frees_o3a(self, op_url: str) -> None:  # noqa: F811
        """``04`` §5.5 and §8.6 together: a plan nobody confirmed lapses at 10:00, and then the day
        is O3-A's if its range breaks."""
        bars = _both_bars()
        async with _rolled_back(op_url) as session:
            user_id = await _seed_expiry(
                session,
                bars=bars,
                until=F.at(DAY, 10, 21),
                chains=(F.at(DAY, 9, 45), F.at(DAY, 10, 20)),
            )
            await plan_o3_minute(
                session, user_id, NOW, trading_day=True, margin=None, mode_of=_paper
            )
            await plan_o3_minute(
                session, user_id, O3A_NOW, trading_day=True, margin=None, mode_of=_paper
            )
            (o3b,) = await _sessions(session, user_id, "O3B")
            (o3a,) = await _sessions(session, user_id, "O3A")
            assert (o3b.state, o3a.state) == ("LAPSED", "PLANNED")
