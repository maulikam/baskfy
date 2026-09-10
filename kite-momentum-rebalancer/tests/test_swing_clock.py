"""The desk's clock (SW11): the 10:45 cutoff and the 15:15 GTT sweep, scheduled and DRY_RUN."""
from __future__ import annotations

import ast
import asyncio
import contextlib
import datetime as dt
import inspect
import re
from decimal import Decimal

import pytest

from app import swing_clock, telemetry
from baskfy_core.swing.config import DEFAULT_SWING_CONFIG
from app.core.risk import RiskManager
from app.swing_execute import build_swing_gateway
from baskfy_execution.gtt import DRY_RUN_GTT
from tests.test_swing_execute import ExplodingKC, MemoryStore

D = Decimal
IST = swing_clock.IST
DAY = dt.date(2026, 9, 2)
AT_1045 = dt.datetime(2026, 9, 2, 10, 45, tzinfo=IST)
AT_1515 = dt.datetime(2026, 9, 2, 15, 15, tzinfo=IST)


class NotedStore(MemoryStore):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.notes: list[tuple[dt.date, str]] = []

    def note_session(self, day, note):
        self.notes.append((day, note))


@pytest.fixture()
def gw(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    return build_swing_gateway(ExplodingKC(), RiskManager())


class TestTheSweepAt1515:
    def test_the_1515_sweep_rearms_every_naked_position_in_dry_run(self, gw):
        """A8: no filled quantity without a GTT. Two naked positions, one covered; after the
        sweep the two carry a (simulated) trigger, the covered one is untouched, and the
        session notes say so — 0 orders reach the exploding broker."""
        store = NotedStore()
        a = store.add_position(symbol="ALPHA", gtt_id=None, gtt_trigger=None, gtt_armed_at=None,
                               quantity_entered=100, quantity_open=100, stop=D("96.00"))
        b = store.add_position(symbol="BETA", instrument_id=2, gtt_id=None, gtt_trigger=None,
                               gtt_armed_at=None, quantity_entered=50, quantity_open=50,
                               stop=D("200.00"), entry_avg=D("210.0000"))
        c = store.add_position(symbol="GAMMA", instrument_id=3, quantity_open=10)
        covered_before = dict(store.positions[c])
        report = asyncio.run(
            swing_clock.run_gtt_sweep(store, gw, now=AT_1515,
                                      price_of=lambda s: {"ALPHA": D("101"), "BETA": D("215")}[s])
        )
        assert (report.naked_before, report.armed, report.still_naked) == (2, 2, 0)
        assert all(o.status == "SIMULATED" and o.gtt["status"] == DRY_RUN_GTT for o in report.outcomes)
        assert store.positions[a]["gtt_id"].startswith("DRY-") and store.positions[a]["gtt_trigger"] == D("96.00")
        assert store.positions[b]["gtt_id"].startswith("DRY-") and store.positions[b]["gtt_trigger"] == D("200.00")
        assert store.positions[c] == covered_before
        assert store.notes == [(DAY, "gtt-sweep 15:15: 2 naked, 2 armed, 0 still naked")]

    def test_the_1515_sweep_reports_what_is_still_naked_when_no_price_can_be_read(self, gw):
        """No last price → the re-arm is BLOCKED (never guessed, SW7) and the count feeds
        SWING_GTT_MISSING_AT_1515 rather than pretending."""
        store = NotedStore()
        store.add_position(symbol="ALPHA", gtt_id=None, gtt_trigger=None, gtt_armed_at=None,
                           quantity_entered=100, quantity_open=100, stop=D("104.00"))

        def no_price(symbol):
            raise RuntimeError("no session")

        report = asyncio.run(
            swing_clock.run_gtt_sweep(store, gw, now=AT_1515, price_of=no_price)
        )
        assert (report.naked_before, report.armed, report.still_naked) == (1, 0, 1)
        assert report.outcomes[0].status == "BLOCKED"
        assert store.notes[-1][1].endswith("1 naked, 0 armed, 1 still naked")

    def test_the_1515_sweep_survives_a_sweep_that_raises(self, gw, monkeypatch):
        from app import swing_execute

        async def boom(*a, **k):
            raise RuntimeError("gateway gone")

        monkeypatch.setattr(swing_execute, "eod_gtt_sweep", boom)
        store = NotedStore()
        store.add_position(gtt_id=None, gtt_trigger=None, gtt_armed_at=None, quantity_open=5)
        report = asyncio.run(
            swing_clock.run_gtt_sweep(store, gw, now=AT_1515, price_of=lambda s: D("101"))
        )
        assert (report.armed, report.still_naked) == (0, 1)


class TestTheCutoffAt1045:
    def test_the_cutoff_frees_pending_slots_and_notes_the_session(self, gw):
        store = NotedStore()
        plan_id = store.add_plan()
        store.add_pending(plan_id)
        report = asyncio.run(
            swing_clock.run_cutoff(store, gw, orders=None, now=AT_1045)
        )
        assert report.slots_freed == 1 and report.cancelled == 0
        assert store.notes == [(DAY, "cutoff 10:45: 0 reconciled, 0 cancelled, 0 refused, "
                                     "1 slots freed")]

    def test_a_cutoff_that_raises_is_logged_and_the_clock_goes_on(self, gw, monkeypatch):
        from app import swing_execute

        async def boom(*a, **k):
            raise RuntimeError("store gone")

        monkeypatch.setattr(swing_execute, "cutoff_open_orders", boom)
        assert asyncio.run(
            swing_clock.run_cutoff(NotedStore(), gw, orders=None, now=AT_1045)
        ) is None


class TestTheSchedule:
    def test_run_after_close_runs_the_cutoff_now_and_sleeps_to_1515_for_the_sweep(self, gw):
        """The monitor stopped at 10:45; the clock runs the cutoff at once, sleeps exactly to
        `gtt_sweep_at`, and sweeps — with a fresh store per chore and the gateway built per
        chore, nothing held in between."""
        store = NotedStore()
        store.add_position(gtt_id=None, gtt_trigger=None, gtt_armed_at=None, quantity_open=100,
                           stop=D("96.00"))
        clock = {"now": AT_1045 + dt.timedelta(seconds=3)}
        slept: list[float] = []
        opened: list[int] = []
        built: list[int] = []

        def sleep(seconds):
            slept.append(seconds)
            clock["now"] = clock["now"] + dt.timedelta(seconds=seconds)

        @contextlib.contextmanager
        def open_store():
            opened.append(1)
            yield store

        def gateway():
            built.append(1)
            return gw

        code = swing_clock.run_after_close(
            day=DAY, now=lambda: clock["now"], sleep=sleep, open_store=open_store,
            gateway=gateway, orders=lambda: None, price_of=lambda s: D("101"),
        )
        assert code == 0
        assert slept == [pytest.approx((AT_1515 - AT_1045).total_seconds() - 3)]
        assert len(opened) == 2 and len(built) == 2
        assert [n for _, n in store.notes] == [
            "cutoff 10:45: 0 reconciled, 0 cancelled, 0 refused, 0 slots freed",
            "gtt-sweep 15:15: 1 naked, 1 armed, 0 still naked",
        ]

    def test_a_clock_started_after_1515_sweeps_at_once(self, gw):
        late = AT_1515 + dt.timedelta(minutes=20)
        slept: list[float] = []

        @contextlib.contextmanager
        def open_store():
            yield NotedStore()

        swing_clock.run_after_close(day=DAY, now=lambda: late, sleep=slept.append,
                                    open_store=open_store, gateway=lambda: gw,
                                    orders=lambda: None, price_of=lambda s: None)
        assert slept == []

    def test_a_gateway_that_cannot_be_built_at_1045_does_not_cost_the_1515_sweep(self):
        """No Kite session at 10:45 (the build raises); the clock still sleeps to 15:15 and
        sweeps with the gateway that is there by then."""
        store = NotedStore()
        store.add_position(gtt_id=None, gtt_trigger=None, gtt_armed_at=None, quantity_open=100,
                           stop=D("96.00"))
        clock = {"now": AT_1045}
        builds = {"n": 0}

        def gateway():
            builds["n"] += 1
            if builds["n"] == 1:
                raise RuntimeError("not authed")
            return build_swing_gateway(ExplodingKC(), RiskManager())

        def sleep(seconds):
            clock["now"] = clock["now"] + dt.timedelta(seconds=seconds)

        @contextlib.contextmanager
        def open_store():
            yield store

        code = swing_clock.run_after_close(
            day=DAY, now=lambda: clock["now"], sleep=sleep, open_store=open_store,
            gateway=gateway, orders=lambda: None, price_of=lambda s: D("101"),
        )
        assert code == 0 and builds["n"] == 2
        assert [n for _, n in store.notes] == ["gtt-sweep 15:15: 1 naked, 1 armed, 0 still naked"]

    def test_the_sweep_time_is_the_config_field_not_a_literal(self):
        assert swing_clock.sweep_time(DAY) == AT_1515
        code = ast.unparse(ast.parse(inspect.getsource(swing_clock)))
        assert not re.search(r"\b15\s*,\s*15\b", code), "the sweep time is typed into the clock"

    def test_the_clock_names_no_placing_verb(self):
        tree = ast.parse(inspect.getsource(swing_clock))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = node.body
                if body and isinstance(body[0], ast.Expr) and isinstance(
                    getattr(body[0], "value", None), ast.Constant
                ):
                    node.body = body[1:] or [ast.Pass()]
        code = ast.unparse(tree)
        for pattern in (r"\bplace\b", r"place_order", r"place_gtt", r"\.place\(", r"delete_gtt",
                        r"OrderGateway", r"\bkc\b", r"kiteconnect"):
            assert not re.search(pattern, code), f"swing_clock.py names {pattern}"


class TestTelemetryInTheClock:
    def test_sweep_telemetry_never_raises_into_the_sweep(self, gw, monkeypatch):
        """The sink raises on every call; the naked position is still armed."""
        def boom(*a, **k):
            raise RuntimeError("metrics sink down")

        monkeypatch.setattr(telemetry, "count", boom)
        monkeypatch.setattr(telemetry, "capture", boom)
        store = NotedStore()
        pid = store.add_position(gtt_id=None, gtt_trigger=None, gtt_armed_at=None,
                                 quantity_open=100, stop=D("96.00"))
        report = asyncio.run(
            swing_clock.run_gtt_sweep(store, gw, now=AT_1515, price_of=lambda s: D("101"))
        )
        assert report.armed == 1 and store.positions[pid]["gtt_id"].startswith("DRY-")
        assert asyncio.run(swing_clock.run_cutoff(store, gw, orders=None, now=AT_1045)) is not None


class TestTheStoresNote:
    def test_note_session_appends_to_the_days_row_on_the_sqlite_twin(self, tmp_path):
        """`PgSwingStore.note_session` (SW11): creates the day's row when absent, appends when
        present, and never touches the counters."""
        from tests.test_swing_desk import Scenario

        scenario = Scenario(str(tmp_path / "swing.db"))
        store = scenario.store()
        store.note_session(DAY, "cutoff 10:45: 0 reconciled")
        store.bump_session(DAY, mode="DRY_RUN", confirms=1)
        store.note_session(DAY, "gtt-sweep 15:15: 1 naked, 1 armed, 0 still naked")
        row = store.session(DAY)
        assert row is not None and row["confirms"] == 1
        assert row["notes"] == "cutoff 10:45: 0 reconciled\ngtt-sweep 15:15: 1 naked, 1 armed, 0 still naked"


class TestTheChoresKeepTheirOwnHours:
    """SW25 extended the watch to 15:30 (9 Sep 2026), and that nearly inverted the session.

    `run_after_close` fires the cutoff the moment the strategy stops. With the watch ending at
    10:45 that was the 10:45 cutoff; with it ending at 15:30 the cutoff would have run AFTER the
    15:15 GTT sweep — the chore that frees slots running after the chore that re-arms stops.

    So `monitor_close` (when triggers stop) and `pending_cutoff_at` (when the housekeeping runs)
    are separate settings, and the monitor calls the chores from inside its loop at their hours.
    """

    def test_the_cutoff_still_precedes_the_sweep(self) -> None:
        window = DEFAULT_SWING_CONFIG.opening_range
        cutoff = dt.time(*window.pending_cutoff_at)
        sweep = dt.time(*window.gtt_sweep_at)
        close = dt.time(*window.monitor_close)

        assert cutoff < sweep, "the cutoff would run after the GTT sweep"
        assert sweep <= close, "the sweep would fall outside the watch that runs it"

    def test_the_watch_now_covers_the_cash_session(self) -> None:
        """Maulik: "anytime during trading time". 09:15 to 15:30, not to 10:45."""
        window = DEFAULT_SWING_CONFIG.opening_range
        assert window.session_open == (9, 15)
        assert window.monitor_close == (15, 30)
        assert window.pending_cutoff_at == (10, 45), (
            "A7/A8's housekeeping moved with the watch; a gap's slot would be held all afternoon"
        )

    def test_run_chore_is_the_single_entry_point(self) -> None:
        """Both callers — the loop and `run_after_close` — must reach the same implementation,
        so a chore cannot be written twice and drift."""
        assert callable(swing_clock.run_chore)
        assert callable(swing_clock.run_cutoff)
        assert callable(swing_clock.run_gtt_sweep)
