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

from app import config as C
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
    """Records every statement `PgSignalStore` runs and hands back ids for RETURNING — and
    answers `load_context`'s four reads from canned rows, so the store's per-trigger re-read
    (SW10.4) sees the book a test puts there: `market` is last night's `sw_market_daily` row,
    `capital` the sleeve, `held` the open positions, `taken` today's confirmed BUY lines,
    `stats` the detection rows. Only the table named in the SQL decides the answer."""

    def __init__(self, *, gate: str = "GREEN", rung: int = 3, capital: str = "1000000",
                 stats: dict[str, tuple[str, int, str]] | None = None,
                 first_live_left: int = 0) -> None:
        self.statements: list[tuple[str, tuple]] = []
        self._next = 100
        self.first_live_left = first_live_left
        #: SW10.5: today's PENDING_RANGE lines still PROPOSED (symbols), and the watch rows'
        #: own ADR / score for names with no detection row.
        self.reserved: list[str] = []
        self.watch_stats: dict[str, tuple[str, str]] = {}
        #: The UPDATE that releases a reservation answers these ids (A7).
        self.releasable: list[int] = []
        tiers = {0: (2, 25.0), 1: (4, 50.0), 2: (6, 75.0), 3: (10, 100.0)}
        count, pct = tiers[rung]
        self.market: dict | None = {
            "gate": gate, "exposure_level": rung, "max_open_positions": count,
            "max_exposure_pct": pct, "new_entries_allowed": gate != "RED",
            "drawdown_locked": False,
        }
        self.capital = capital
        self.held: list[dict] = []
        self.taken: list[dict] = []
        self.stats = {"AAA": ("5.00", 100_000_000, "72.00"), **(stats or {})}
        self.context_reads = 0

    def execute(self, sql: str, params=()):
        flat = " ".join(sql.split())
        self.statements.append((flat, tuple(params)))
        conn = self
        one: dict | None = None
        many: list[dict] = []
        if flat.startswith("SELECT gate,") and "sw_market_daily" in flat:
            one = conn.market
            conn.context_reads += 1
        elif flat.startswith("SELECT sleeve_capital_inr"):
            one = {"sleeve_capital_inr": conn.capital,
                   "first_live_sessions_left": conn.first_live_left}
        elif "FROM public.sw_position p" in flat:
            many = list(conn.held)
        elif "FROM public.sw_plan_line l" in flat and "PENDING_RANGE" in flat:
            many = [{"symbol": symbol} for symbol in conn.reserved]
        elif "FROM public.sw_plan_line l" in flat:
            many = [{"entered": None, **row} for row in conn.taken]
        elif "FROM public.sw_setup_daily s" in flat:
            many = [{"symbol": k, "adr_pct": v[0], "turnover_avg": v[1], "score": v[2]}
                    for k, v in conn.stats.items()]
        elif "FROM public.sw_watch w" in flat and "adr_pct" in flat:
            many = [{"symbol": k, "adr_pct": v[0], "score": v[1]}
                    for k, v in conn.watch_stats.items()]
        elif flat.startswith("UPDATE public.sw_plan_line SET state = 'SKIPPED'"):
            many = [{"id": i} for i in conn.releasable]
            conn.releasable = []

        class _Cur:
            def fetchone(self_inner):
                if "RETURNING id" in sql:
                    conn._next += 1
                    return {"id": conn._next}
                return one

            def fetchall(self_inner):
                return many

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


def _store(conn: FakeConn) -> swing_monitor.PgSignalStore:
    """The store as `main` and the drill build it: the config, and the context read from the
    connection — no context handed in, because since SW10.4 it is re-read per trigger."""
    return swing_monitor.PgSignalStore(conn, user_id=1, day=DAY, config=DEFAULT_SWING_CONFIG)


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
        store = _store(conn)
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
        store = _store(conn)
        store.raise_signal(_signal(_name("LCK", 3, circuit="52.5"),
                                   TriggerState.LOCKED_UPPER_CIRCUIT, entry=None, stop=None))
        assert len(conn.inserted("sw_signal")) == 1
        assert conn.inserted("sw_signal")[0][6] == "LOCKED_UPPER_CIRCUIT"
        assert conn.inserted("sw_plan") == []
        assert conn.inserted("sw_plan_line") == []

    def test_under_a_red_gate_the_trigger_is_recorded_and_the_plan_says_why_not(self):
        """The rules apply to a live trigger as they apply to a planned one: a red gate is a
        skip with its reason, never a line."""
        conn = FakeConn(gate="RED")
        store = _store(conn)
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


# --- SW10.4 (STANDING-ANSWERS A5): the context is re-read per trigger -------------------------


class TestTheContextIsReReadPerTrigger:
    """SW10.2's finding, closed: the second SIGNAL plan of a morning is sized against a book
    that includes the first's confirm. Before SW10.4 `SignalContext` was read once at 09:15
    and two confirms could overshoot the rung's ceiling (34 % of a 25 % rung in the drill)."""

    def test_load_context_counts_positions_at_cost_and_todays_confirmed_lines(self):
        conn = FakeConn(rung=0)
        conn.held = [{"symbol": "AAA", "entry_avg": 100.8, "quantity_open": 1666}]
        conn.taken = [
            # a live order accepted and not filled (SW7.1): held at the trigger, no position
            {"id": 7, "symbol": "BBB", "quantity": 100, "trigger": 210.0, "state": "SENT",
             "position_id": None},
            # a filled line whose position is the one above: counted once, at cost
            {"id": 8, "symbol": "AAA", "quantity": 1666, "trigger": 100.8, "state": "FILLED",
             "position_id": 1},
        ]
        ctx = swing_monitor.load_context(conn, user_id=1, day=DAY)
        assert ctx.account.open_symbols == frozenset({"AAA", "BBB"})
        assert ctx.account.open_exposure_inr == Decimal("100.8") * 1666 + Decimal(210) * 100
        assert ctx.account.cash_available == Decimal(1_000_000) - ctx.account.open_exposure_inr
        assert ctx.entries_today == 2, "both lines were entries today, whatever became of them"
        assert [p.symbol for p in ctx.pending] == ["BBB"]
        assert ctx.tier.level == 0 and ctx.tier.max_exposure_pct == 25.0

    def test_load_context_with_no_market_row_is_red_and_rung_zero(self):
        conn = FakeConn()
        conn.market = None
        ctx = swing_monitor.load_context(conn, user_id=1, day=DAY)
        assert ctx.gate.value == "RED" and ctx.tier.new_entries_allowed is False

    def test_the_second_signal_plan_of_a_morning_is_sized_against_the_first_confirm(self):
        """Per-trigger re-read: at rung 0 (2 names, 25 % of ₹10 lakh = ₹2.5 lakh) the 09:31
        AAA line is 1,666 × 100.80 = ₹1,67,932.80; once that is confirmed and filled, the
        09:45 BBB trigger at 210 sees ₹82,067.20 of headroom — and is lined for the 390 shares
        that fit it (A5's re-size, `entries_now`), not the 833 a 09:15 reading would have
        given: the book after both is ₹2,49,832.80, 24.98 % of the sleeve, under the 25 %."""
        conn = FakeConn(rung=0, stats={"BBB": ("6.00", 200_000_000, "70.00")})
        store = _store(conn)
        store.raise_signal(_signal(_name("AAA", 1), TriggerState.TRIGGERED,
                                   entry="100.80", stop="97.80"))
        (first,) = conn.inserted("sw_plan_line")
        assert first[5] == 1666
        reads_after_first = conn.context_reads
        # The person confirms AAA on the desk: a position at cost, a FILLED line.
        conn.held = [{"symbol": "AAA", "entry_avg": 100.8, "quantity_open": 1666}]
        conn.taken = [{"id": conn._next, "symbol": "AAA", "quantity": 1666, "trigger": 100.8,
                       "state": "FILLED", "position_id": 1}]
        store.raise_signal(_signal(_name("BBB", 2, pivot="205"), TriggerState.TRIGGERED,
                                   entry="210.00", stop="204.00"))
        assert conn.context_reads > reads_after_first, "the context was not re-read per trigger"
        first_line, second = conn.inserted("sw_plan_line")
        assert second[5] == 390, second
        assert second[9] == Decimal("81900.00"), "position_value = 390 × 210"
        assert conn.inserted("sw_plan_skip") == []
        book = Decimal("100.80") * 1666 + second[9]
        assert book == Decimal("249832.80") and book <= Decimal(250_000)
        assert store.context.account.open_symbols == frozenset({"AAA"})
        assert store.context.entries_today == 1

    def test_a_trigger_that_fits_no_sliver_is_exposure_full_with_the_arithmetic(self):
        """The re-size has a floor: the headroom must still buy the minimum trade value
        (`04` §5.1, ₹10,000). ₹2,000 of headroom is `EXPOSURE_FULL`, and the skip's detail
        shows the book, the ceiling and the headroom so the page can say why."""
        conn = FakeConn(rung=0, stats={"BBB": ("6.00", 200_000_000, "70.00")})
        conn.held = [{"symbol": "AAA", "entry_avg": 124.0, "quantity_open": 2000}]  # ₹2.48 lakh
        store = _store(conn)
        store.raise_signal(_signal(_name("BBB", 2, pivot="205"), TriggerState.TRIGGERED,
                                   entry="210.00", stop="204.00"))
        assert conn.inserted("sw_plan_line") == []
        (skip,) = conn.inserted("sw_plan_skip")
        assert skip[4] == "EXPOSURE_FULL"
        assert "leaves ₹2,000.00" in skip[5] and "BELOW_MIN_TRADE_VALUE" in skip[5], skip[5]

    def test_the_third_entry_of_a_session_is_the_cap_and_the_fourth_is_session_cap(self):
        """`04` §5.3 at the monitor: three names confirmed today (whatever plan they came from)
        and the next trigger is `SESSION_CAP`, before its size is even looked at."""
        conn = FakeConn(rung=3, stats={"DDD": ("5.00", 100_000_000, "60.00")})
        conn.taken = [
            {"id": i, "symbol": s, "quantity": 10, "trigger": 100.0, "state": st,
             "position_id": None}
            for i, (s, st) in enumerate((("AAA", "FILLED"), ("BBB", "SENT"), ("CCC", "CONFIRMED")))
        ]
        store = _store(conn)
        store.raise_signal(_signal(_name("DDD", 4), TriggerState.TRIGGERED,
                                   entry="100.80", stop="97.80"))
        assert conn.inserted("sw_plan_line") == []
        assert [k[4] for k in conn.inserted("sw_plan_skip")] == ["SESSION_CAP"]

    def test_the_store_still_accepts_a_starting_context_but_never_sizes_from_it(self):
        """`main` and the drill hand the 09:15 reading in for the log; a plan is sized from
        the re-read, so a stale generous context cannot line what the book refuses."""
        from baskfy_core.swing.market import ExposureTier, MarketGate
        from baskfy_core.swing.plan import SwingAccount
        stale = swing_monitor.SignalContext(
            gate=MarketGate.GREEN,
            tier=ExposureTier(level=3, max_open_positions=10, max_exposure_pct=100.0,
                              new_entries_allowed=True),
            account=SwingAccount(equity=Decimal(1_000_000), cash_available=Decimal(1_000_000),
                                 open_symbols=frozenset(), open_exposure_inr=Decimal(0)),
            detected={"AAA": (Decimal("5.00"), Decimal(100_000_000), Decimal("72.00"))},
        )
        conn = FakeConn(gate="RED")
        store = swing_monitor.PgSignalStore(conn, user_id=1, day=DAY, config=DEFAULT_SWING_CONFIG,
                                            context=stale)
        store.raise_signal(_signal(_name("AAA", 1), TriggerState.TRIGGERED,
                                   entry="100.80", stop="97.80"))
        assert conn.inserted("sw_plan_line") == []
        assert [k[4] for k in conn.inserted("sw_plan_skip")] == ["GATE_RED"]
        assert store.context.gate is MarketGate.RED, "the store's context is the last reading"

    def test_load_config_hands_the_plan_the_persons_sizing_knobs(self):
        """SW9.5.3 for the monitor: `sw_config.risk_per_trade_pct` / `max_position_pct` /
        `max_open_positions` reach `SizingConfig`, so a SIGNAL line previews the confirm."""
        class _Conn:
            def execute(self, sql, params=()):
                class _Cur:
                    def fetchone(self_inner):
                        return {"adr_min_pct": 4.5, "turnover_min_inr": 60_000_000.0,
                                "price_min": 25.0, "risk_per_trade_pct": 0.25,
                                "max_position_pct": 15.0, "max_open_positions": 4}
                return _Cur()
        config = swing_monitor.load_config(_Conn(), user_id=1)
        assert (config.sizing.risk_per_trade_pct, config.sizing.max_position_pct,
                config.sizing.max_open_positions) == (0.25, 15.0, 4)
        assert config.liquidity.adr_min_pct == 4.5
        assert config.stops == DEFAULT_SWING_CONFIG.stops, "only the person's knobs move"


# =======================================================================================
# SW10.5 — STANDING-ANSWERS A7 (reserved slots released once) and A9 (half risk at the SIGNAL
# plan) in the monitor's context and store.
# =======================================================================================
class TestReservedSlotsAndHalfRisk:
    def test_load_context_carries_the_reserved_slots_the_first_live_count_and_the_watch_adr(self):
        conn = FakeConn(first_live_left=3)
        conn.reserved = ["GAPCO"]
        conn.watch_stats = {"GAPCO": ("6.19", "46.20")}
        context = swing_monitor.load_context(conn, user_id=1, day=DAY)
        assert context.reserved == ("GAPCO",)
        assert context.first_live_sessions_left == 3
        assert context.detected["GAPCO"] == (Decimal("6.19"), None, Decimal("46.20"))
        assert context.detected["AAA"][0] == Decimal("5.00"), "a detection row still wins"

    def test_a_reserved_slot_counts_against_the_session_cap_for_other_names(self):
        """A7: two entries taken plus one reserved slot is the whole session; the next trigger
        of another name is SESSION_CAP — the gap crowded it out, as MD10 wants."""
        conn = FakeConn(rung=3)
        conn.taken = [
            {"id": 1, "symbol": "AAA", "quantity": 100, "trigger": 100.0, "state": "FILLED",
             "position_id": 1},
            {"id": 2, "symbol": "BBB", "quantity": 100, "trigger": 100.0, "state": "FILLED",
             "position_id": 2},
        ]
        conn.held = [{"symbol": "AAA", "entry_avg": 100.0, "quantity_open": 100},
                     {"symbol": "BBB", "entry_avg": 100.0, "quantity_open": 100}]
        conn.reserved = ["GAPCO"]
        store = _store(conn)
        store.raise_signal(_signal(_name("CCC", 3), TriggerState.TRIGGERED,
                                   entry="100.80", stop="97.80"))
        assert conn.inserted("sw_plan_line") == []
        (skip,) = conn.inserted("sw_plan_skip")
        assert skip[4] == "SESSION_CAP"

    def test_the_reserved_name_itself_is_not_counted_against_its_own_slot(self):
        """The gap's own trigger: its reservation is released first and never counted
        against it, so with two entries taken it is still lined as the third."""
        conn = FakeConn(rung=3, stats={"GAPCO": ("6.19", 100_000_000, "46.20")})
        conn.taken = [
            {"id": 1, "symbol": "AAA", "quantity": 100, "trigger": 100.0, "state": "FILLED",
             "position_id": 1},
            {"id": 2, "symbol": "BBB", "quantity": 100, "trigger": 100.0, "state": "FILLED",
             "position_id": 2},
        ]
        conn.held = [{"symbol": "AAA", "entry_avg": 100.0, "quantity_open": 100},
                     {"symbol": "BBB", "entry_avg": 100.0, "quantity_open": 100}]
        conn.reserved = ["GAPCO"]
        conn.releasable = [77]
        store = _store(conn)
        store.raise_signal(_signal(_name("GAPCO", 9, setup=Setup.EP, pivot=None),
                                   TriggerState.TRIGGERED, entry="112.50", stop="107.50"))
        (line,) = conn.inserted("sw_plan_line")
        assert line[2] == "BUY_ON_TRIGGER" and line[5] > 0
        releases = [s for s, _ in conn.statements
                    if s.startswith("UPDATE public.sw_plan_line SET state = 'SKIPPED'")]
        assert len(releases) == 1, "released exactly once, before the plan was sized"
        assert "user_id = ?" in releases[0] and "kind = 'PENDING_RANGE'" in releases[0]
        assert "state = 'PROPOSED'" in releases[0]

    def test_a_wide_stop_on_a_live_gap_is_stop_too_wide_and_the_slot_is_released(self):
        """A7: stop wider than one ADR → SIZE_REFUSED / STOP_TOO_WIDE; the reservation was
        released on the trigger, so nothing holds the slot afterwards."""
        conn = FakeConn(rung=3)
        conn.watch_stats = {"GAPCO": ("5.00", "46.20")}
        conn.reserved = ["GAPCO"]
        conn.releasable = [77]
        store = _store(conn)
        store.raise_signal(_signal(_name("GAPCO", 9, setup=Setup.EP, pivot=None),
                                   TriggerState.TRIGGERED, entry="112.50", stop="105.00"))
        assert conn.inserted("sw_plan_line") == []
        (skip,) = conn.inserted("sw_plan_skip")
        assert (skip[4], skip[5]) == ("SIZE_REFUSED", "STOP_TOO_WIDE")
        assert store.release_reservation(90, dt.datetime(2026, 8, 19, 9, 40)) == 0

    def test_a_live_gap_with_no_detection_row_is_sized_off_the_watch_rows_adr(self):
        """SW9.5.2's gap closed for live gaps: the watch row's ADR (6.19) admits a 4.4% stop."""
        conn = FakeConn(rung=3)
        conn.watch_stats = {"GAPCO": ("6.19", "46.20")}
        store = _store(conn)
        store.raise_signal(_signal(_name("GAPCO", 9, setup=Setup.EP, pivot=None),
                                   TriggerState.TRIGGERED, entry="112.50", stop="107.50"))
        (line,) = conn.inserted("sw_plan_line")
        assert line[2] == "BUY_ON_TRIGGER" and line[5] == 1000

    def test_the_signal_plan_is_half_risk_only_while_counting_down_and_live(self, monkeypatch):
        """A9 at the monitor: 1,666 → 833 with the count at 5 and a real order ahead; 1,666
        with the count at 5 on paper; 1,666 with the count at 0 and live."""
        sizes = []
        for left, live in ((5, True), (5, False), (0, True)):
            monkeypatch.setattr(C, "SWING_EXECUTION_ENABLED", live)
            monkeypatch.setattr(C, "DRY_RUN", not live)
            conn = FakeConn(rung=3, first_live_left=left)
            store = _store(conn)
            store.raise_signal(_signal(_name("AAA", 1), TriggerState.TRIGGERED,
                                       entry="100.80", stop="97.80"))
            (line,) = conn.inserted("sw_plan_line")
            sizes.append(line[5])
        assert sizes == [833, 1666, 1666]

    def test_the_context_counts_a_partial_fills_open_remainder_as_exposure(self):
        """A8: a SENT line with 40 of 100 filled — a position for 40 and 60 still resting —
        counts 40 at cost and 60 at the trigger, so the next confirm sizes against both."""
        conn = FakeConn(rung=3)
        conn.held = [{"symbol": "AAA", "entry_avg": 100.0, "quantity_open": 40}]
        conn.taken = [{"id": 1, "symbol": "AAA", "quantity": 100, "trigger": 101.0,
                       "state": "SENT", "position_id": 1, "entered": 40}]
        context = swing_monitor.load_context(conn, user_id=1, day=DAY)
        assert context.account.open_exposure_inr == Decimal("4000") + Decimal("6060")
        assert context.entries_today == 1 and context.pending == ()
