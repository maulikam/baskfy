"""SW6's desk half: the opening-range monitor raises signals and never places.

`docs/swing/06` SW6's acceptance, in its own words:

* "with the flag off the strategy is not instantiated";
* "the replay of the fixture morning raises exactly the expected signals with the expected
  entry/stop";
* "a locked name yields a `LOCKED_UPPER_CIRCUIT` signal and no plan line".

Plus the one `docs/swing/02` Track C makes non-negotiable and SW10 will scan for again: the
strategy has no path to an order. Asserted over its *source* here, not over a mock that could
be forgotten — `on_tick` never touches `self.gw`, `generate_targets` is `[]`, and the file never
names a placing verb.
"""
from __future__ import annotations

import ast
import asyncio
import datetime as dt
import inspect
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from app import swing_monitor
from app.strategies import swing_breakout
from app.strategies.swing_breakout import Signal, SwingBreakout, WatchedName
from baskfy_core.swing.config import DEFAULT_SWING_CONFIG, Setup
from baskfy_core.swing.opening_range import TriggerState

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tools" / "swing" / "fixtures"
if str(ROOT / "tools" / "swing") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools" / "swing"))

import replay  # noqa: E402 - the harness under test, on the path above

DAY = dt.date(2026, 8, 19)


def _code_only(source: str) -> str:
    """The source with every docstring and comment removed — what *runs*, not what explains.

    The modules under test explain at length why they never place, and a scan that failed on
    the word "place" in that explanation would be scanning the wrong thing.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.body and isinstance(node.body[0], ast.Expr) and isinstance(
                getattr(node.body[0], "value", None), ast.Constant
            ) and isinstance(node.body[0].value.value, str):
                node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(tree)


# --- the seams ---------------------------------------------------------------------------


class ListStore:
    def __init__(self) -> None:
        self.signals: list[Signal] = []

    def raise_signal(self, signal: Signal) -> None:
        self.signals.append(signal)


class NoCandles:
    """A candle source with nothing — the tick fallback's case."""

    def minute_candles(self, token, day, until):
        return []


class FakeConn:
    """Records every statement `PgSignalStore` runs and hands back ids for RETURNING."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple]] = []
        self._next = 100

    def execute(self, sql: str, params=()):
        self.statements.append((" ".join(sql.split()), tuple(params)))
        conn = self

        class _Cur:
            def fetchone(self_inner):
                if "RETURNING id" in sql:
                    conn._next += 1
                    return {"id": conn._next}
                return None

            def fetchall(self_inner):
                return []

        return _Cur()

    def inserted(self, table: str) -> list[tuple]:
        return [p for s, p in self.statements if s.startswith(f"INSERT INTO public.{table} ")]


def _name(symbol: str, token: int, *, setup: Setup = Setup.FLAG, pivot: str | None = "100",
          circuit: str | None = None) -> WatchedName:
    return WatchedName(
        watch_id=token, instrument_id=token * 10, symbol=symbol, token=token, setup=setup,
        pivot_high=Decimal(pivot) if pivot else None,
        upper_circuit=Decimal(circuit) if circuit else None,
    )


def _tick(token: int, price: float, hhmm: str, *, low: float | None = None) -> dict:
    hour, minute = (int(x) for x in hhmm.split(":"))
    return {
        "instrument_token": token,
        "last_price": price,
        "exchange_timestamp": dt.datetime(2026, 8, 19, hour, minute),
        "ohlc": {"low": low if low is not None else price},
    }


def _drive(strategy: SwingBreakout, ticks: list[dict]) -> None:
    async def _go():
        for t in ticks:
            await strategy.on_tick(t)
    asyncio.run(_go())


# --- the flag ----------------------------------------------------------------------------


class TestTheFlag:
    def test_with_the_flag_off_the_strategy_is_not_instantiated(self, monkeypatch):
        built: list[object] = []
        original = SwingBreakout.__init__

        def spy(self, *a, **k):
            built.append(self)
            original(self, *a, **k)

        monkeypatch.setattr(SwingBreakout, "__init__", spy)
        result = swing_monitor.build_monitor(
            enabled=False, gateway=None, watchlist=[_name("AAA", 1)], store=ListStore(),
            candles=NoCandles(), day=DAY,
        )
        assert result is None
        assert built == [], "the flag is off and a SwingBreakout was constructed anyway"

    def test_with_the_flag_on_it_is(self):
        result = swing_monitor.build_monitor(
            enabled=True, gateway=None, watchlist=[_name("AAA", 1)], store=ListStore(),
            candles=NoCandles(), day=DAY,
        )
        assert isinstance(result, SwingBreakout)
        assert result.tokens == [1]

    def test_the_process_default_is_off(self):
        """`BASKFY_SWING_MONITOR_ENABLED` defaults false (SW2's boundary), and `main` honours
        it before touching a database, a broker or a bus."""
        from app import config as C
        assert C.SWING_MONITOR_ENABLED is False
        source = inspect.getsource(swing_monitor.main)
        assert source.index("SWING_MONITOR_ENABLED") < source.index("from .analytics.db import")


# --- never an order ----------------------------------------------------------------------


class TestItCannotPlace:
    def test_generate_targets_is_empty(self):
        strategy = SwingBreakout(None, watchlist=[_name("AAA", 1)], store=ListStore(),
                                 candles=NoCandles(), day=DAY)
        assert asyncio.run(strategy.generate_targets({"anything": True})) == []

    def test_the_strategy_never_names_the_gateway_or_a_placing_verb(self):
        source = _code_only(inspect.getsource(swing_breakout))
        for forbidden in ("self.gw", "gw.place", ".place(", "place_order", "place_gtt",
                          "kc.", "kiteconnect", "kite_client"):
            assert forbidden not in source, f"swing_breakout.py names {forbidden}"

    def test_the_runner_never_names_a_placing_verb_either(self):
        source = _code_only(inspect.getsource(swing_monitor))
        for forbidden in ("gw.place", ".place(", "place_order", "place_gtt", "OrderGateway("):
            assert forbidden not in source, f"swing_monitor.py names {forbidden}"

    def test_the_monitor_is_built_with_no_gateway_at_all(self):
        """`main` hands `build_monitor` `gateway=None`: a process that holds no gateway cannot
        be talked into using one."""
        source = inspect.getsource(swing_monitor.main)
        assert "gateway=None" in source


# --- the replay --------------------------------------------------------------------------


class TestTheReplay:
    def test_the_fixture_morning_raises_exactly_the_expected_signals(self):
        day, window, watchlist = replay.read_watchlist(FIXTURES / "morning-synthetic.watchlist.json")
        candles = replay.read_candles(FIXTURES / "morning-synthetic.csv")
        signals = replay.replay(
            ticks=replay.ticks_from_candles(candles), candles=candles, watchlist=watchlist,
            day=day, window_minutes=window,
        )
        raised = [replay.Raised.of(s) for s in signals]
        expected = [
            replay.Raised.from_json(e)
            for e in json.loads((FIXTURES / "morning-synthetic.expected.json").read_text())
        ]
        assert raised == expected

    def test_the_harness_exit_code_says_so(self, capsys):
        code = replay.main([
            str(FIXTURES / "morning-synthetic.csv"),
            "--watchlist", str(FIXTURES / "morning-synthetic.watchlist.json"),
            "--expect", str(FIXTURES / "morning-synthetic.expected.json"),
        ])
        assert code == 0
        assert "OK" in capsys.readouterr().out

    def test_the_expected_entry_and_stop_are_the_rules_not_the_fixture(self):
        """Why 100.80 / 97.80: the first tick over the 09:15-09:19 range high (99.5 x 1.001)
        that is also above the pivot (100) is 09:31's open, and the stop is the lower of the
        range low (98.0) and the low of the day (97.8 at 09:22) — `04` §7.2."""
        expected = json.loads((FIXTURES / "morning-synthetic.expected.json").read_text())
        alpha = next(e for e in expected if e["symbol"] == "ALPHAFLAG")
        assert (alpha["entry"], alpha["stop"]) == ("100.80", "97.80")


# --- the strategy's own rules --------------------------------------------------------------


class TestTheStrategy:
    def test_a_name_triggers_once_and_is_then_done_for_the_session(self):
        store = ListStore()
        strategy = SwingBreakout(None, watchlist=[_name("AAA", 1, pivot="100")], store=store,
                                 candles=NoCandles(), day=DAY)
        _drive(strategy, [
            _tick(1, 99.0, "09:15"), _tick(1, 98.0, "09:17", low=98.0), _tick(1, 99.5, "09:19"),
            _tick(1, 99.6, "09:20"),               # window closes; tick range is 98.0-99.6
            _tick(1, 100.5, "09:25"),              # > 99.6 x 1.001 and > pivot: TRIGGERED
            _tick(1, 101.0, "09:26"), _tick(1, 102.0, "09:30"),
        ])
        states = [s.verdict.state for s in store.signals]
        assert states == [TriggerState.TRIGGERED]
        assert store.signals[0].verdict.entry == Decimal("100.5")
        assert store.signals[0].verdict.stop == Decimal("98.0")

    def test_a_locked_name_yields_one_locked_signal_and_never_triggers(self):
        store = ListStore()
        strategy = SwingBreakout(None, watchlist=[_name("LCK", 3, pivot="50", circuit="52.5")],
                                 store=store, candles=NoCandles(), day=DAY)
        _drive(strategy, [_tick(3, 52.5, f"09:{m:02d}") for m in range(15, 40)])
        assert [s.verdict.state for s in store.signals] == [TriggerState.LOCKED_UPPER_CIRCUIT]
        assert all(s.verdict.entry is None for s in store.signals)

    def test_a_tick_for_a_token_not_on_the_list_is_ignored(self):
        store = ListStore()
        strategy = SwingBreakout(None, watchlist=[_name("AAA", 1)], store=store,
                                 candles=NoCandles(), day=DAY)
        _drive(strategy, [_tick(99, 1000.0, "09:30")])
        assert store.signals == []
        assert 99 not in strategy.state

    def test_after_monitor_close_nothing_is_raised(self):
        store = ListStore()
        strategy = SwingBreakout(None, watchlist=[_name("AAA", 1, pivot="100")], store=store,
                                 candles=NoCandles(), day=DAY)
        _drive(strategy, [_tick(1, 99.0, "09:15"), _tick(1, 99.0, "09:20"),
                          _tick(1, 150.0, "10:46")])
        assert store.signals == []
        assert strategy.session_over(dt.datetime(2026, 8, 19, 10, 46))
        assert not strategy.session_over(dt.datetime(2026, 8, 19, 10, 45))

    def test_a_failing_store_does_not_stop_the_monitor(self):
        class Broken:
            def raise_signal(self, signal):
                raise RuntimeError("db down")

        strategy = SwingBreakout(None, watchlist=[_name("AAA", 1, pivot="100")], store=Broken(),
                                 candles=NoCandles(), day=DAY)
        _drive(strategy, [_tick(1, 99.0, "09:15"), _tick(1, 99.0, "09:20"), _tick(1, 101.0, "09:25")])
        assert [s.verdict.state for s in strategy.signals] == [TriggerState.TRIGGERED]

    def test_the_window_must_be_one_the_contract_names(self):
        """`04` §7.1 names (1, 5, 60); a seven-minute range is refused at start-up, not at
        09:22 when it would never close."""
        with pytest.raises(ValueError, match="window"):
            SwingBreakout(None, watchlist=[_name("AAA", 1)], store=ListStore(),
                          candles=NoCandles(), day=DAY, window_minutes=7)


# --- the store ---------------------------------------------------------------------------


def _context(*, gate: str = "GREEN") -> swing_monitor.SignalContext:
    from baskfy_core.swing.market import ExposureTier, MarketGate
    from baskfy_core.swing.plan import SwingAccount
    return swing_monitor.SignalContext(
        gate=MarketGate(gate),
        tier=ExposureTier(level=3, max_open_positions=8, max_exposure_pct=100.0,
                          new_entries_allowed=gate != "RED"),
        account=SwingAccount(equity=Decimal(1_000_000), cash_available=Decimal(1_000_000),
                             open_symbols=frozenset(), open_exposure_inr=Decimal(0)),
        detected={"AAA": (Decimal("5.00"), Decimal(100_000_000), Decimal("72.00"))},
    )


def _signal(name: WatchedName, state: TriggerState, *, entry: str | None, stop: str | None) -> Signal:
    from baskfy_core.swing.opening_range import TriggerVerdict
    return Signal(
        watch=name, at=dt.datetime(2026, 8, 19, 9, 31),
        verdict=TriggerVerdict(state, Decimal(entry) if entry else None,
                               Decimal(stop) if stop else None, Decimal("99.5"), Decimal("98.0")),
        last_price=Decimal(entry) if entry else Decimal("52.5"),
        low_of_day=Decimal("97.8"), window_minutes=5,
    )


class TestTheSignalStore:
    def test_a_trigger_is_a_signal_row_and_a_one_line_signal_plan(self):
        conn = FakeConn()
        store = swing_monitor.PgSignalStore(conn, user_id=1, day=DAY,
                                            config=DEFAULT_SWING_CONFIG, context=_context())
        store.raise_signal(_signal(_name("AAA", 1), TriggerState.TRIGGERED,
                                   entry="100.80", stop="97.80"))
        assert len(conn.inserted("sw_signal")) == 1
        assert len(conn.inserted("sw_plan")) == 1
        lines = conn.inserted("sw_plan_line")
        assert len(lines) == 1
        (params,) = lines
        # kind, quantity, trigger, stop — `04` §5: 0.5 % of ₹10 lakh over a ₹3 stop is 1,666
        # by risk, and the 20 % position cap at ₹100.80 is 1,984, so risk wins.
        assert params[2] == "BUY_ON_TRIGGER"
        assert params[5] == 1666
        assert (params[6], params[7]) == (Decimal("100.80"), Decimal("97.80"))
        assert params[-1].endswith(":AAA:BUY_ON_TRIGGER"), "client_id = plan_id:symbol:kind"
        plan_params = conn.inserted("sw_plan")[0]
        assert plan_params[3] + dt.timedelta(minutes=30) == plan_params[4], "30-minute expiry"
        assert store.lines_written == [conn._next]

    def test_a_locked_name_is_a_signal_row_and_no_plan_line(self):
        conn = FakeConn()
        store = swing_monitor.PgSignalStore(conn, user_id=1, day=DAY,
                                            config=DEFAULT_SWING_CONFIG, context=_context())
        store.raise_signal(_signal(_name("LCK", 3, circuit="52.5"),
                                   TriggerState.LOCKED_UPPER_CIRCUIT, entry=None, stop=None))
        assert len(conn.inserted("sw_signal")) == 1
        assert conn.inserted("sw_signal")[0][6] == "LOCKED_UPPER_CIRCUIT"
        assert conn.inserted("sw_plan") == []
        assert conn.inserted("sw_plan_line") == []

    def test_under_a_red_gate_the_trigger_is_recorded_and_the_plan_says_why_not(self):
        """The rules apply to a live trigger as they apply to a planned one: a red gate is a
        skip with its reason, never a line."""
        conn = FakeConn()
        store = swing_monitor.PgSignalStore(conn, user_id=1, day=DAY,
                                            config=DEFAULT_SWING_CONFIG, context=_context(gate="RED"))
        store.raise_signal(_signal(_name("AAA", 1), TriggerState.TRIGGERED,
                                   entry="100.80", stop="97.80"))
        assert len(conn.inserted("sw_signal")) == 1
        assert len(conn.inserted("sw_plan")) == 1
        assert conn.inserted("sw_plan_line") == []
        (skip,) = conn.inserted("sw_plan_skip")
        assert skip[4] == "GATE_RED"

    def test_the_store_never_names_a_placing_verb(self):
        source = _code_only(inspect.getsource(swing_monitor.PgSignalStore)).lower()
        for forbidden in (".place(", "place_order", "place_gtt", "kc.", "gateway", "order_"):
            assert forbidden not in source, forbidden
