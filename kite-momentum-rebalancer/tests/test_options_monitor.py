"""OP9 — the desk's options monitor (`docs/options/06` OP9).

`06` OP9's acceptance criteria, each with its test:

* flag off → not instantiated (`TestTheFlag`);
* the O1 fixture replays to `PROFIT` at the expected minute, O2's to `TIME_STOP`, O3-A's to
  `TARGET`, and the index feed dying at 14:05 with O1 open → `HARD_EXIT / FEED_LOST`
  (`TestTheReplays` — the expectation is `tools/options/make_fixtures.py`'s independent answer key,
  not the replay's own output);
* a restart at 11:30 resumes from `op_position` (`TestTheRestart`);
* a source scan finds no `place(` in the modules (`TestNothingPlaces`).

And what the loop and the store must do for those to hold on the box: the idle pass that judges a
silent feed, the quote fallback's throttle, following new legs, one exit per position, and the
store's SQL against a real PostgreSQL (`TestTheStoreOnADatabase`, skipped without one).
"""
from __future__ import annotations

import ast
import importlib.util
import asyncio
import datetime as dt
import inspect
import json
import os
import pathlib
import re
import sys
import uuid
from decimal import Decimal
from types import ModuleType

import pytest

from app import config as C
from app import options_clock, options_monitor
from app.options_clock import LegQuotes, run_session
from app.options_monitor import PgPositionStore, build_monitor, position_from_rows
from app.strategies import nifty_options
from app.strategies.nifty_options import NIFTY_50_TOKEN, NiftyOptionsMonitor, TrackedPosition

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tools" / "options" / "fixtures"


def _load_harness() -> ModuleType:
    """`tools/options/replay.py`, loaded under its own name. The swing suite imports
    `tools/swing/replay.py` as plain `replay`; putting this one on `sys.path` under the same name
    made whichever loaded second read the other's harness (found when the whole desk suite ran)."""
    spec = importlib.util.spec_from_file_location(
        "options_replay", ROOT / "tools" / "options" / "replay.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["options_replay"] = module
    spec.loader.exec_module(module)
    return module


replay = _load_harness()

SCENARIOS = ("o1-profit", "o1-restart-1130", "o1-feed-lost", "o2-time-stop", "o3a-target")


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def _expected(name: str) -> list[dict]:
    return json.loads((FIXTURES / f"{name}.expected.json").read_text())


def _code_only(source: str) -> str:
    """The source without docstrings — what runs, not what explains (the swing suite's rule)."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(
                first.value.value, str
            ):
                node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(tree)


# --- the flag ------------------------------------------------------------------------------------


class TestTheFlag:
    def test_flag_off_the_strategy_is_never_constructed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        built: list[object] = []
        original = NiftyOptionsMonitor.__init__

        def spy(self: NiftyOptionsMonitor, *args: object, **kwargs: object) -> None:
            built.append(self)
            original(self, *args, **kwargs)

        monkeypatch.setattr(NiftyOptionsMonitor, "__init__", spy)
        store = replay.ListStore([], [])
        assert build_monitor(enabled=False, gateway=None, store=store, day=dt.date(2026, 10, 27)) is None
        assert built == []
        assert isinstance(
            build_monitor(enabled=True, gateway=None, store=store, day=dt.date(2026, 10, 27)),
            NiftyOptionsMonitor,
        )

    def test_the_flag_defaults_off(self) -> None:
        assert C.OPTIONS_MONITOR_ENABLED is False

    def test_main_checks_the_flag_before_importing_the_world(self) -> None:
        source = inspect.getsource(options_monitor.main)
        flag = source.index("C.OPTIONS_MONITOR_ENABLED")
        for heavy in ("from .analytics.db import", "from .core.ticker import", "from .kite_client"):
            assert flag < source.index(heavy), heavy

    def test_main_returns_zero_with_the_flag_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(C, "OPTIONS_MONITOR_ENABLED", False)
        assert options_monitor.main() == 0

    def test_the_runner_holds_no_gateway(self) -> None:
        assert "gateway=None" in inspect.getsource(options_monitor.main)


# --- the acceptance replays ----------------------------------------------------------------------


class TestTheReplays:
    @pytest.mark.parametrize("name", SCENARIOS)
    def test_each_fixture_raises_exactly_the_expected_exit(self, name: str) -> None:
        _, store = replay.replay(_fixture(name))
        assert replay.as_json(store.exits) == _expected(name)

    def test_the_expectations_are_the_ones_06_names(self) -> None:
        """The answer key says what `06` OP9 asks — checked here, so a regenerated key that
        drifted would fail rather than quietly agree with a drifted monitor."""
        codes = {name: [(e["code"], e["reason"]) for e in _expected(name)] for name in SCENARIOS}
        assert codes["o1-profit"] == [("PROFIT", "RULE")]
        assert codes["o2-time-stop"] == [("TIME_STOP", "RULE")]
        assert codes["o3a-target"] == [("TARGET", "RULE")]
        assert codes["o1-feed-lost"] == [("HARD_EXIT", "FEED_LOST")]
        feed = _expected("o1-feed-lost")[0]["at"]
        assert feed.startswith("2026-10-27T14:05")  # "the index feed dying at 14:05"
        # O2's time stop is 45 minutes after its 10:06 entry, on the first tick at or after it.
        assert _expected("o2-time-stop")[0]["at"] == "2026-10-19T10:51:00"

    @pytest.mark.parametrize("name", SCENARIOS)
    def test_the_cli_agrees(self, name: str) -> None:
        args = [str(FIXTURES / f"{name}.json"), "--expect", str(FIXTURES / f"{name}.expected.json")]
        assert replay.main(args) == 0

    def test_one_exit_per_position_whatever_follows(self) -> None:
        """After PROFIT the position is exiting: the rest of the day raises nothing more."""
        monitor, store = replay.replay(_fixture("o1-profit"))
        assert len(store.exits) == len(monitor.exits) == 1

    def test_marks_are_recorded_every_30_seconds_at_most(self) -> None:
        _, store = replay.replay(_fixture("o3a-target"))
        stamps = [at for _, _, at in store.marks]
        assert stamps and all(
            (b - a).total_seconds() >= 30 for a, b in zip(stamps, stamps[1:], strict=False)
        )


# --- the restart ---------------------------------------------------------------------------------


class TestTheRestart:
    def test_a_restart_at_1130_resumes_from_op_position(self) -> None:
        fixture = _fixture("o1-restart-1130")
        monitor, store = replay.replay(fixture)
        # The position came from the store (op_position), not from any tick before 11:30 ...
        assert [t.session_id for t in store.positions] == [1]
        # ... the morning's bars came back through op_index_minute ...
        bars = monitor.bars()
        assert bars[0].ts == dt.datetime(2026, 10, 27, 9, 15)
        # ... and the resumed process raised the exit the rules give from 11:30.
        assert replay.as_json(store.exits) == _expected("o1-restart-1130")

    def test_a_position_already_exiting_is_not_evaluated_again(self) -> None:
        fixture = _fixture("o1-profit")
        tracked = replay.position_of(fixture["position"])
        store = replay.ListStore([TrackedPosition(tracked.session_id, tracked.position, "X-1")], [])
        monitor = NiftyOptionsMonitor(None, store=store, day=dt.date(2026, 10, 27))

        async def run() -> None:
            await monitor.on_start()
            for tick in replay.ticks_of(fixture):
                await monitor.on_tick(tick)

        asyncio.run(run())
        assert store.exits == [] and monitor.exits == []


# --- nothing places ------------------------------------------------------------------------------


class TestNothingPlaces:
    @pytest.mark.parametrize("module", [nifty_options, options_monitor, options_clock])
    def test_no_placing_verb_in_the_code(self, module: object) -> None:
        code = _code_only(inspect.getsource(module))
        # Whole words: `dataclasses.replace(` is not a placing verb, `place(` / `.place(` is.
        for verb in (r"\bplace\(", r"\bplace_order\b", r"\bplace_gtt", r"\bOrderGateway\(",
                     r"\bgw\.", r"\bkc\.place"):
            assert re.search(verb, code) is None, verb

    def test_the_strategy_targets_nothing(self) -> None:
        monitor = NiftyOptionsMonitor(None, store=replay.ListStore([], []), day=dt.date(2026, 10, 27))
        assert asyncio.run(monitor.generate_targets({})) == []
        assert monitor.gw is None

    def test_the_quote_fallback_uses_the_slot_taking_read(self) -> None:
        code = _code_only(inspect.getsource(options_clock))
        assert "quote_raw(" in code and "kc.quote(" not in code


# --- the loop, the quotes, the tokens ------------------------------------------------------------


class FakeKite:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def quote_raw(self, keys: list[str]) -> dict:
        self.calls.append(keys)
        return {
            key: {
                "instrument_token": int(key),
                "last_price": 25010.0,
                "depth": {"buy": [{"price": 10.0}], "sell": [{"price": 10.2}]},
                "timestamp": dt.datetime(2026, 10, 27, 14, 6),
            }
            for key in keys
        }


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class TestTheQuoteFallback:
    def test_at_most_one_call_per_interval(self) -> None:
        clock = Clock()
        kite = FakeKite()
        quotes = LegQuotes(kite, min_interval=5.0, clock=clock)
        now = dt.datetime(2026, 10, 27, 14, 6)
        assert quotes.poll([NIFTY_50_TOKEN, 9001], now)
        clock.t = 4.9
        assert quotes.poll([NIFTY_50_TOKEN, 9001], now) is None
        clock.t = 5.0
        assert quotes.poll([NIFTY_50_TOKEN, 9001], now)
        assert kite.calls == [["256265", "9001"], ["256265", "9001"]]

    def test_a_quote_becomes_a_depth_tick(self) -> None:
        (tick, _) = LegQuotes(FakeKite(), clock=Clock()).poll([9001, 9002], dt.datetime(2026, 10, 27))
        assert tick["instrument_token"] == 9001
        assert tick["depth"]["buy"][0]["price"] == 10.0

    def test_a_failed_call_is_counted_and_empty(self) -> None:
        class Broken:
            def quote_raw(self, keys: list[str]) -> dict:
                raise RuntimeError("kite down")

        quotes = LegQuotes(Broken(), clock=Clock())
        assert quotes.poll([9001], dt.datetime(2026, 10, 27)) == []
        assert quotes.failures == 1


class Bus:
    def __init__(self) -> None:
        self.queues: dict[int, asyncio.Queue] = {}

    def subscribe(self, token: int) -> asyncio.Queue:
        self.queues[token] = asyncio.Queue()
        return self.queues[token]


class TestTheLoop:
    def test_an_idle_pass_judges_a_silent_feed(self) -> None:
        """No tick at all after 14:04:45: the loop's own clock must raise FEED_LOST."""
        fixture = _fixture("o1-feed-lost")
        tracked = replay.position_of(fixture["position"])
        store = replay.ListStore([tracked], [])
        monitor = NiftyOptionsMonitor(None, store=store, day=dt.date(2026, 10, 27))
        ticks = [t for t in replay.ticks_of(fixture) if t["exchange_timestamp"] < dt.datetime(2026, 10, 27, 14, 5)]
        times = iter(
            [dt.datetime(2026, 10, 27, 14, 5, 30), dt.datetime(2026, 10, 27, 14, 5, 30)]
            + [dt.datetime(2026, 10, 27, 15, 31)] * 4
        )

        async def run() -> int:
            await monitor.on_start()
            for tick in ticks:
                await monitor.on_tick(tick)
            store_before = len(store.exits)
            assert store_before == 0
            return await run_session(
                monitor, Bus(), now=lambda: next(times), sleep=lambda _s: asyncio.sleep(0)
            )

        raised = asyncio.run(run())
        assert raised == 1
        assert [(c, r) for _, c, r, _ in store.exits] == [("HARD_EXIT", "FEED_LOST")]

    def test_new_legs_are_followed_on_the_ticker(self) -> None:
        fixture = _fixture("o3a-target")
        tracked = replay.position_of(fixture["position"])
        store = replay.ListStore([], [])
        monitor = NiftyOptionsMonitor(
            None, store=store, day=dt.date(2026, 10, 20), refresh_seconds=0
        )
        followed: list[list[int]] = []
        moments = iter(
            [dt.datetime(2026, 10, 20, 10, 23)] * 3 + [dt.datetime(2026, 10, 20, 15, 31)] * 3
        )

        def now() -> dt.datetime:
            moment = next(moments)
            if moment.minute == 23 and not store.positions:
                store.positions.append(tracked)  # the executor opened it (OP10)
            return moment

        asyncio.run(
            run_session(
                monitor, Bus(), follow=followed.append, now=now,
                sleep=lambda _s: asyncio.sleep(0),
            )
        )
        assert followed[0] == [NIFTY_50_TOKEN]
        assert sorted(followed[1]) == [9201, 9202]

    def test_the_loop_ends_at_the_close(self) -> None:
        monitor = NiftyOptionsMonitor(None, store=replay.ListStore([], []), day=dt.date(2026, 10, 27))
        raised = asyncio.run(
            run_session(monitor, Bus(), now=lambda: dt.datetime(2026, 10, 27, 15, 30))
        )
        assert raised == 0


# --- the store -----------------------------------------------------------------------------------


def _head(sleeve: str, structure: str, detail: dict) -> dict:
    return {
        "session_id": 7,
        "entry_points": Decimal("30.00"),
        "opened_at": dt.datetime(2026, 10, 20, 10, 22),
        "exit_plan_id": None,
        "leg_ids": [1, 2],
        "sleeve": sleeve,
        "structure": structure,
        "risk_budget_inr": Decimal("2500.00"),
        "width_points": Decimal("100.00"),
        "detail": detail,
    }


LEGS = [
    {"id": 1, "role": "LONG_CALL", "strike": Decimal("25200"), "quantity": 65, "filled_qty": 65,
     "avg_price": Decimal("35.05"), "instrument_token": 9201},
    {"id": 2, "role": "SHORT_CALL", "strike": Decimal("25300"), "quantity": 65, "filled_qty": 65,
     "avg_price": Decimal("5.00"), "instrument_token": 9202},
]


class TestTheRowsBecomeAPosition:
    def test_o3a_reads_the_morning_range(self) -> None:
        detail = {"direction": "UP", "gate": {"range_high": "25182.00", "range_low": "24998.00"}}
        tracked = position_from_rows(_head("O3A", "DEBIT_SPREAD", detail), LEGS)
        p = tracked.position
        assert (p.range_high, p.range_low, p.width_points) == (
            Decimal("25182.00"), Decimal("24998.00"), Decimal("100.00"),
        )
        assert [lg.fill_price for lg in p.legs] == [Decimal("35.05"), Decimal("5.00")]
        assert tracked.tokens == (9201, 9202)

    def test_o2_reads_the_opening_range_and_o3b_the_half_gap(self) -> None:
        o2 = position_from_rows(
            _head("O2", "LONG_OPTION", {"direction": "UP", "gate": {"or_high": "25032", "or_low": "25008"}}),
            LEGS[:1],
        ).position
        assert (o2.range_high, o2.range_low) == (Decimal("25032"), Decimal("25008"))
        o3b = position_from_rows(
            _head("O3B", "DEBIT_SPREAD", json.dumps({"direction": "UP", "gate": {"half_gap": "25100.00"}})),
            LEGS,
        ).position
        assert o3b.half_gap == Decimal("25100.00")


def _pg_url() -> str | None:
    url = os.environ.get("BASKFY_TEST_DATABASE_URL")
    return None if not url else url.replace("postgresql+asyncpg://", "postgresql://")


@pytest.mark.skipif(_pg_url() is None, reason="BASKFY_TEST_DATABASE_URL is not set")
class TestTheStoreOnADatabase:
    """The store's SQL on a real PostgreSQL with the `op_` schema (the screener's migrations)."""

    def _seed(self, conn: object) -> tuple[int, int]:
        c = conn  # the desk's pg.Connection: `?` placeholders, dict rows
        uid = c.execute(
            "INSERT INTO app_user (public_id, email) VALUES (?, ?) RETURNING id",
            (f"op9-{uuid.uuid4().hex[:8]}", f"op9-{uuid.uuid4().hex[:8]}@x.test"),
        ).fetchone()["id"]
        day = dt.date(2026, 10, 20)
        for token, strike in ((9201, 25200), (9202, 25300)):
            c.execute(
                "INSERT INTO op_contract (instrument_token, tradingsymbol, underlying, expiry, "
                "strike, option_type, lot_size, tick_size, first_seen, last_seen, expired) "
                "VALUES (?, ?, 'NIFTY', ?, ?, 'CE', 65, 0.05, ?, ?, false) "
                "ON CONFLICT (instrument_token) DO NOTHING",
                (token, f"NIFTYOP9{token}", day, strike, day, day),
            )
        sid = c.execute(
            "INSERT INTO op_session (user_id, sleeve, trade_date, expiry_used, mode, state, plan_id) "
            "VALUES (?, 'O3A', ?, ?, 'PAPER', 'OPEN', ?) RETURNING id",
            (uid, day, day, f"O3A-OP9-{uid}"),
        ).fetchone()["id"]
        detail = json.dumps({"direction": "UP", "gate": {"range_high": "25182.00", "range_low": "24998.00"}})
        plan_pk = c.execute(
            "INSERT INTO op_plan (user_id, plan_id, session_id, sleeve, structure, kind, sizing_mode, "
            "issued_at, expires_at, lots, lot_size, width_points, risk_budget_inr, status, detail) "
            "VALUES (?, ?, ?, 'O3A', 'DEBIT_SPREAD', 'ENTRY', 'PAPER_ONE_LOT', ?, ?, 1, 65, 100, 2500, "
            "'CONFIRMED', ?) RETURNING id",
            (uid, f"O3A-OP9-{uid}", sid, dt.datetime(2026, 10, 20, 10, 21, tzinfo=options_monitor.IST),
             dt.datetime(2026, 10, 20, 10, 51, tzinfo=options_monitor.IST), detail),
        ).fetchone()["id"]
        leg_ids = []
        for seq, (role, token, strike, side, price) in enumerate(
            (("LONG_CALL", 9201, 25200, "BUY", "35.05"), ("SHORT_CALL", 9202, 25300, "SELL", "5.00")),
            start=1,
        ):
            leg_ids.append(c.execute(
                "INSERT INTO op_leg (user_id, plan_id, seq, role, tradingsymbol, instrument_token, "
                "strike, option_type, side, quantity, status, filled_qty, avg_price) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'CE', ?, 65, 'FILLED', 65, ?) RETURNING id",
                (uid, plan_pk, seq, role, f"NIFTYOP9{token}", token, strike, side, price),
            ).fetchone()["id"])
        c.execute(
            "INSERT INTO op_position (session_id, user_id, leg_ids, entry_points, entry_inr, lots, "
            "opened_at, hard_exit_at, simulated) VALUES (?, ?, ?, 30.05, 1953.25, 1, ?, ?, true)",
            (sid, uid, leg_ids, dt.datetime(2026, 10, 20, 10, 22, tzinfo=options_monitor.IST),
             dt.datetime(2026, 10, 20, 14, 45, tzinfo=options_monitor.IST)),
        )
        return int(uid), int(sid)

    def test_open_positions_raise_exit_and_mark(self) -> None:
        from app.analytics.pg import Connection  # noqa: PLC0415 - psycopg only with a database

        url = _pg_url()
        assert url is not None
        conn = Connection(url)
        uid, sid = self._seed(conn)
        try:
            store = PgPositionStore(conn, user_id=uid)
            (tracked,) = store.open_positions(dt.date(2026, 10, 20))
            assert tracked.session_id == sid and tracked.exit_plan_id is None
            assert tracked.position.entry_points == Decimal("30.05")
            assert tracked.position.range_high == Decimal("25182.00")
            assert [lg.instrument_token for lg in tracked.position.legs] == [9201, 9202]
            verdict = nifty_options.ExitVerdict("TARGET", "RULE", Decimal("81.50"), Decimal("-3315"), False)
            at = dt.datetime(2026, 10, 20, 10, 40, 50)
            first = store.raise_exit(tracked, verdict, at)
            again = store.raise_exit(tracked, verdict, at)
            assert first == again == f"O3A-OP9-{uid}-X"
            rows = conn.execute(
                "SELECT kind, status, detail FROM op_plan WHERE plan_id = ?", (first,)
            ).fetchall()
            assert len(rows) == 1 and rows[0]["kind"] == "EXIT" and rows[0]["status"] == "ISSUED"
            assert rows[0]["detail"]["code"] == "TARGET"
            (reread,) = store.open_positions(dt.date(2026, 10, 20))
            assert reread.exit_plan_id == first
            store.record_mark(tracked, Decimal("81.5"), at)
            mark = conn.execute(
                "SELECT last_mark_points FROM op_position WHERE session_id = ?", (sid,)
            ).fetchone()
            assert Decimal(str(mark["last_mark_points"])) == Decimal("81.50")
        finally:
            conn.execute("DELETE FROM app_user WHERE id = ?", (uid,))
            conn.close()
