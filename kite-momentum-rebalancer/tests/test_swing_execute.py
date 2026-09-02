"""SW7's execute logic, driven through the REAL gateway against a broker client that explodes.

Every test in this module builds its gateway with `build_swing_gateway` over `ExplodingKC`,
whose `place_order`, `place_gtt`, `delete_gtt` and `instruments` raise `AssertionError`. The
swing execution flag is false (its default), so the whole path — guards, risk, rate limits,
idempotency, journal — runs and ends in the gateway's dry-run branch. If any test here ever
reached a broker it would fail with "reached the broker", which is the SW7 acceptance criterion
stated as a test: **0 orders reach a broker in the whole suite.**

The store is an in-memory dict-of-dicts implementing the `SwingStore` Protocol, so these tests
assert `docs/swing/02` (the law) and `04` §5, §6, §9, §10 (the numbers) rather than any SQL.
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import copy
import datetime as dt
import inspect
import json
import pathlib
import uuid
from decimal import Decimal

import pytest
from baskfy_core.swing.journal import ClosedTrade
from baskfy_core.swing.market import ExposureTier, MarketGate
from baskfy_core.swing.plan import SwingAccount
from baskfy_core.swing.sizing import r_multiple
from baskfy_execution.gtt import DRY_RUN_GTT, DRY_RUN_GTT_DELETE
from fastapi import HTTPException

from app import config as C
from app import swing_execute as X
from app.core.risk import RiskManager

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
NOW = dt.datetime(2026, 9, 2, 9, 31, tzinfo=IST)
D = Decimal


# --- the fakes ---------------------------------------------------------------------------


class ExplodingKC:
    """A broker client no test may touch. Every method that could reach Zerodha explodes."""

    VARIETY_REGULAR = "regular"
    TRANSACTION_TYPE_BUY, TRANSACTION_TYPE_SELL = "BUY", "SELL"
    PRODUCT_CNC, ORDER_TYPE_LIMIT, ORDER_TYPE_MARKET = "CNC", "LIMIT", "MARKET"
    VALIDITY_DAY, GTT_TYPE_SINGLE = "DAY", "single"

    def place_order(self, **_: object) -> str:
        raise AssertionError("an order reached the broker")

    def place_gtt(self, **_: object) -> dict:
        raise AssertionError("a GTT reached the broker")

    def delete_gtt(self, **_: object) -> None:
        raise AssertionError("a GTT cancel reached the broker")

    def instruments(self, *_: object) -> list:
        raise AssertionError("the instrument dump was fetched — a live GTT path ran")


#: The ladder's rungs, `04` §8.4 — (max open positions, max exposure %), lowest first.
TIERS = ((2, 25.0), (4, 50.0), (6, 75.0), (10, 100.0))


class MemoryStore:
    """`SwingStore` over dicts. Every write is kept so a test can read what the book became.

    The confirm-time gate (SW10.4) reads its context from the same dicts: the book is the
    positions with shares open plus today's CONFIRMED/SENT lines not yet positions; the gate,
    the rung and each name's ADR are the store's `market` and `detected` — a test sets them the
    way a scenario writes `sw_market_daily` and `sw_setup_daily`. The lock is a snapshot: the
    dicts are copied on entry and restored on an exception, the in-memory twin of a rollback.
    """

    def __init__(self, *, first_live_sessions_left: int = 0, rung: int = 3,
                 gate: str = "GREEN", capital: Decimal = D("1000000")) -> None:
        self.plans: dict[str, dict] = {}
        self.lines: dict[int, dict] = {}
        self.positions: dict[int, dict] = {}
        self.fills: list[dict] = []
        self.sessions: dict[dt.date, dict] = {}
        self.line_history: list[tuple[int, str]] = []
        self._config = {
            "sleeve_capital_inr": capital,
            "risk_per_trade_pct": D("0.500"),
            "max_position_pct": D("20.00"),
            "max_open_positions": 10,
            "first_live_sessions_left": first_live_sessions_left,
            "exposure_level": rung,
        }
        self.first_live_writes: list[int] = []
        self.market = {"gate": gate, "rung": rung, "new_entries_allowed": gate != "RED",
                       "drawdown_locked": False}
        #: symbol → (adr_pct, avg_turnover_inr, score), as the latest detection row would say.
        self.detected: dict[str, tuple[Decimal, Decimal | None, Decimal]] = {
            "ALPHA": (D("5.00"), D(100_000_000), D("72.00")),
        }
        self.locks: list[dt.date] = []
        self.resizes: list[dict] = []
        self.context_reads = 0

    # -- setup helpers (not part of the protocol) --
    def add_plan(self, *, built_at: dt.datetime = NOW - dt.timedelta(minutes=5)) -> str:
        plan_id = str(uuid.uuid4())
        self.plans[plan_id] = {
            "id": len(self.plans) + 1, "plan_id": plan_id, "as_of": built_at.date(),
            "source": "MORNING", "built_at": built_at,
            "expires_at": built_at + dt.timedelta(minutes=30), "gate": "GREEN",
            "exposure_level": 1,
        }
        return plan_id

    def add_line(self, plan_id: str, **fields) -> int:
        line_id = len(self.lines) + 1
        base = {
            "id": line_id, "plan_pk": self.plans[plan_id]["id"], "plan_id": plan_id,
            "kind": "BUY_ON_TRIGGER", "instrument_id": 11, "symbol": "ALPHA", "setup": "FLAG",
            "quantity": 100, "trigger": D("100.00"), "stop": D("96.00"),
            "risk_inr": D("400.00"), "position_value": D("10000.00"), "trail": "MA20",
            "note": "", "state": "PROPOSED", "client_id": f"{plan_id}:ALPHA:BUY_ON_TRIGGER",
        }
        base.update(fields)
        self.lines[line_id] = base
        # A line exists because a detection row did (the evening sized it off that row's
        # ADR); the store knows the name unless a test says otherwise (`del store.detected[…]`).
        self.detected.setdefault(base["symbol"], (D("5.00"), D(100_000_000), D("72.00")))
        return line_id

    def add_position(self, **fields) -> int:
        fields.setdefault("symbol", "ALPHA")
        fields.setdefault("instrument_id", 11)
        return self.create_position(fields)

    # -- the protocol --
    def plan(self, plan_id):
        return self.plans.get(plan_id)

    def line(self, line_id):
        return self.lines.get(line_id)

    def set_line(self, line_id, *, state, journal_ref=None, position_id=None):
        row = self.lines[line_id]
        row["state"] = state
        if journal_ref is not None:
            row["journal_ref"] = journal_ref
        if position_id is not None:
            row["position_id"] = position_id
        self.line_history.append((line_id, state))

    def open_position_for(self, instrument_id):
        for row in self.positions.values():
            if (row["instrument_id"] == instrument_id and row["state"] != "CLOSED"
                    and int(row["quantity_open"]) > 0):
                return dict(row)
        return None

    def position(self, position_id):
        row = self.positions.get(position_id)
        return dict(row) if row else None

    def create_position(self, fields):
        position_id = len(self.positions) + 1
        row = {
            "id": position_id, "instrument_id": 11, "symbol": "ALPHA", "setup": "FLAG",
            "entry_date": NOW.date(), "entry_avg": D("100.00"), "quantity_entered": 300,
            "quantity_open": 300, "initial_stop": D("96.00"), "stop": D("96.00"),
            "gtt_id": "DRY-x", "gtt_trigger": D("96.00"), "gtt_armed_at": NOW, "trail": "MA20",
            "partial_done": False, "partial_date": None, "state": "OPEN", "closed_on": None,
            "exit_avg": None, "close_reason": None, "r_multiple": None, "pnl_inr": None,
            "simulated": True,
        }
        row.update(fields)
        row["id"] = position_id
        self.positions[position_id] = row
        return position_id

    def update_position(self, position_id, fields):
        self.positions[position_id].update(fields)

    def add_fill(self, fields):
        self.fills.append(dict(fields))
        return len(self.fills)

    def bump_session(self, day, *, mode, confirms=0, fills=0, manage_actions=0):
        row = self.sessions.setdefault(day, {"mode": mode, "confirms": 0, "fills": 0,
                                             "manage_actions": 0})
        row["mode"] = mode
        row["confirms"] += confirms
        row["fills"] += fills
        row["manage_actions"] += manage_actions

    def config(self):
        return dict(self._config)

    def set_first_live_sessions_left(self, value):
        self._config["first_live_sessions_left"] = value
        self.first_live_writes.append(value)

    # -- the confirm-time gate (SW10.4) --
    @contextlib.contextmanager
    def lock_session_for_update(self, day):
        self.locks.append(day)
        snapshot = copy.deepcopy((self.plans, self.lines, self.positions, self.fills,
                                  self.sessions, self._config))
        try:
            yield
        except BaseException:
            (self.plans, self.lines, self.positions, self.fills, self.sessions,
             self._config) = snapshot
            raise

    def session_context(self, day):
        self.context_reads += 1
        held = [p for p in self.positions.values()
                if p["state"] != "CLOSED" and int(p["quantity_open"]) > 0]
        exposure = sum((D(str(p["entry_avg"])) * int(p["quantity_open"]) for p in held), D(0))
        symbols = {p["symbol"] for p in held}
        taken = [ln for ln in self.lines.values()
                 if ln["kind"] == "BUY_ON_TRIGGER" and ln["state"] in ("CONFIRMED", "SENT", "FILLED")
                 and self.plans[ln["plan_id"]]["as_of"] == day]
        for ln in taken:
            if ln.get("position_id") is None and ln["symbol"] not in symbols:
                exposure += D(str(ln["trigger"])) * int(ln["quantity"])
                symbols.add(ln["symbol"])
        capital = self._config["sleeve_capital_inr"]
        count, pct = TIERS[self.market["rung"]]
        return X.SignalContext(
            gate=MarketGate(self.market["gate"]),
            tier=ExposureTier(level=self.market["rung"], max_open_positions=count,
                              max_exposure_pct=pct,
                              new_entries_allowed=self.market["new_entries_allowed"],
                              drawdown_locked=self.market["drawdown_locked"]),
            account=SwingAccount(equity=capital, cash_available=max(capital - exposure, D(0)),
                                 open_symbols=frozenset(symbols), open_exposure_inr=exposure),
            detected=dict(self.detected),
            entries_today=len(taken),
        )

    def resize_line(self, line_id, *, quantity, risk_inr, position_value, note):
        row = self.lines[line_id]
        row.update({"quantity": quantity, "risk_inr": risk_inr, "position_value": position_value,
                    "note": note})
        self.resizes.append({"line_id": line_id, "quantity": quantity, "risk_inr": risk_inr,
                             "position_value": position_value, "note": note})


@pytest.fixture(autouse=True)
def _flag_off(monkeypatch, tmp_path):
    """The defaults this run ships with, pinned: execution flag false, the journal in tmp."""
    monkeypatch.setattr(C, "SWING_EXECUTION_ENABLED", False)
    monkeypatch.setattr(C, "DRY_RUN", True)
    monkeypatch.chdir(tmp_path)
    X._FIRST_LIVE_COUNTED.clear()


@pytest.fixture()
def gw():
    return X.build_swing_gateway(ExplodingKC(), RiskManager())


def run(coro):
    return asyncio.run(coro)


def journal_events(gw) -> list[str]:
    """The event names the gateway journalled, in order — none when nothing reached it."""
    path = pathlib.Path(gw._journal_path)
    if not path.exists():
        return []
    return [json.loads(row)["event"] for row in path.read_text().splitlines()]


def sent_quantities(gw) -> list[int]:
    """The quantity of every dry-run ORDER the gateway journalled, in order — the size that
    went to the gateway, which the dry-run result itself does not carry."""
    path = pathlib.Path(gw._journal_path)
    if not path.exists():
        return []
    rows = [json.loads(row) for row in path.read_text().splitlines()]
    return [int(r["qty"]) for r in rows if r["event"] == "dry_run"]


def execute(store, gw, plan_id, line_id, *, confirm="true", now=NOW, last_price=None):
    return run(X.execute_line(store, gw, plan_id=plan_id, line_id=line_id, confirm=confirm,
                              now=now, last_price=last_price))


# --- G1: the surface ---------------------------------------------------------------------


def test_contract_surface_by_name() -> None:
    for name in ("SwingStore", "ExecOutcome", "swing_gates", "build_swing_gateway",
                 "execute_line", "rearm_gtt"):
        assert hasattr(X, name), name
    fields = X.ExecOutcome.__dataclass_fields__
    assert list(fields) == ["status", "reason", "order", "gtt", "position_id", "simulated"]


def test_swing_gates_dry_run_unless_both_flags_allow(monkeypatch) -> None:
    """`02` Track B: the flag is the swing book's dry-run, independent of DRY_RUN."""
    monkeypatch.setattr(C, "DRY_RUN", False)
    monkeypatch.setattr(C, "SWING_EXECUTION_ENABLED", False)
    assert X.swing_gates().dry_run is True
    monkeypatch.setattr(C, "DRY_RUN", True)
    monkeypatch.setattr(C, "SWING_EXECUTION_ENABLED", True)
    assert X.swing_gates().dry_run is True
    monkeypatch.setattr(C, "DRY_RUN", False)
    assert X.swing_gates().dry_run is False


def test_swing_gates_never_allow_intraday_or_options(monkeypatch) -> None:
    """Track C §1: CNC only, whatever the weekly desk has been allowed."""
    monkeypatch.setattr(C, "INTRADAY_ENABLED", True)
    monkeypatch.setattr(C, "OPTIONS_ENABLED", True)
    gates = X.swing_gates()
    assert gates.intraday_enabled is False and gates.options_enabled is False


def test_build_swing_gateway_uses_swing_band_and_own_journal(gw) -> None:
    assert gw._stop_band.min_pct == C.SWING_STOP_BAND_MIN
    assert gw._stop_band.max_pct == C.SWING_STOP_BAND_MAX
    assert gw._gates is X.swing_gates
    assert pathlib.Path(gw._journal_path).name == "swing_orders_journal.jsonl"
    import app.core.gateway as shim
    assert pathlib.Path(gw._journal_path).parent == pathlib.Path(shim.JOURNAL).parent, (
        "the swing journal must sit beside the desk's so the conftest isolation covers it"
    )


# --- G2: the four refusals, before anything is touched ------------------------------------


def test_400_when_confirm_is_not_true(gw) -> None:
    store = MemoryStore()
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id)
    for confirm in ("", "false", "True", "yes"):
        with pytest.raises(HTTPException) as err:
            execute(store, gw, plan_id, line_id, confirm=confirm)
        assert err.value.status_code == 400
    assert store.lines[line_id]["state"] == "PROPOSED"
    assert store.sessions == {}


def test_404_unknown_plan(gw) -> None:
    store = MemoryStore()
    with pytest.raises(HTTPException) as err:
        execute(store, gw, "no-such-plan", 1)
    assert err.value.status_code == 404


def test_404_line_not_in_plan(gw) -> None:
    store = MemoryStore()
    p1, p2 = store.add_plan(), store.add_plan()
    line_in_p2 = store.add_line(p2)
    with pytest.raises(HTTPException) as err:
        execute(store, gw, p1, line_in_p2)
    assert err.value.status_code == 404
    with pytest.raises(HTTPException) as err:
        execute(store, gw, p1, 999)
    assert err.value.status_code == 404


def test_410_expired_plan_marks_the_line_expired(gw) -> None:
    store = MemoryStore()
    plan_id = store.add_plan(built_at=NOW - dt.timedelta(minutes=31))
    line_id = store.add_line(plan_id)
    with pytest.raises(HTTPException) as err:
        execute(store, gw, plan_id, line_id)
    assert err.value.status_code == 410
    assert store.lines[line_id]["state"] == "EXPIRED"
    assert store.positions == {} and store.fills == []


def test_expired_boundary_is_thirty_minutes_exactly(gw) -> None:
    store = MemoryStore()
    plan_id = store.add_plan(built_at=NOW - dt.timedelta(minutes=30))
    line_id = store.add_line(plan_id)
    out = execute(store, gw, plan_id, line_id)   # now == expires_at: still good
    assert out.status == "SIMULATED"


def test_409_when_line_is_not_proposed(gw) -> None:
    store = MemoryStore()
    plan_id = store.add_plan()
    for state in ("CONFIRMED", "SENT", "FILLED", "REJECTED", "SKIPPED", "EXPIRED"):
        line_id = store.add_line(plan_id, state=state)
        with pytest.raises(HTTPException) as err:
            execute(store, gw, plan_id, line_id)
        assert err.value.status_code == 409, state


def test_409_a_re_posted_confirm_cannot_send_twice(gw) -> None:
    """Non-negotiable 1, restated for the swing surface: the first confirm fills, the second
    is refused before the gateway is consulted."""
    store = MemoryStore()
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id)
    first = execute(store, gw, plan_id, line_id)
    assert first.status == "SIMULATED"
    with pytest.raises(HTTPException) as err:
        execute(store, gw, plan_id, line_id)
    assert err.value.status_code == 409
    assert len(store.fills) == 1
    assert store.sessions[NOW.date()]["confirms"] == 1


# --- the BUY path -------------------------------------------------------------------------


def test_buy_dry_run_fills_whole_line_and_arms_gtt_in_same_call(gw) -> None:
    store = MemoryStore()
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id)
    out = execute(store, gw, plan_id, line_id)
    assert out.status == "SIMULATED" and out.simulated is True and out.reason == ""
    assert out.order["status"] == "DRY_RUN"
    assert out.order["order_id"] == f"DRY-{plan_id}:ALPHA:BUY"
    assert out.gtt["status"] == DRY_RUN_GTT
    assert out.gtt["qty"] == 100 and out.gtt["trigger"] == 96.0
    pos = store.positions[out.position_id]
    assert pos["quantity_entered"] == pos["quantity_open"] == 100
    assert pos["entry_avg"] == D("100.00") and pos["initial_stop"] == pos["stop"] == D("96.00")
    assert pos["gtt_id"] is not None and pos["gtt_id"].startswith("DRY-")
    assert pos["gtt_trigger"] == D("96.00") and pos["gtt_armed_at"] == NOW
    assert pos["state"] == "OPEN" and pos["simulated"] is True
    assert pos["trail"] == "MA20" and pos["setup"] == "FLAG"
    assert store.fills == [{
        "position_id": out.position_id, "side": "BUY", "quantity": 100,
        "price": D("100.00"), "filled_at": NOW, "journal_ref": out.order["order_id"],
        "simulated": True,
    }]
    line = store.lines[line_id]
    assert line["state"] == "FILLED" and line["position_id"] == out.position_id
    assert line["journal_ref"] == out.order["order_id"]
    assert store.sessions[NOW.date()] == {"mode": "DRY_RUN", "confirms": 1, "fills": 1,
                                          "manage_actions": 0}


def test_buy_line_is_marked_confirmed_before_the_gateway_is_called(gw) -> None:
    store = MemoryStore()
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id)
    execute(store, gw, plan_id, line_id)
    assert store.line_history[0] == (line_id, "CONFIRMED")
    assert store.line_history[-1] == (line_id, "FILLED")


def test_buy_gtt_client_id_is_separate_from_order_client_id(gw) -> None:
    """The gateway keeps two idempotency maps; the ids must differ or the stop is DUPLICATE."""
    store = MemoryStore()
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id)
    out = execute(store, gw, plan_id, line_id)
    assert f"{plan_id}:ALPHA:BUY" in gw._sent
    assert f"{plan_id}:ALPHA:GTT" in gw._gtt_sent
    assert out.gtt["status"] == DRY_RUN_GTT


def test_buy_journal_carries_order_and_gtt_lines(gw) -> None:
    store = MemoryStore()
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id)
    execute(store, gw, plan_id, line_id)
    events = journal_events(gw)
    assert events == ["dry_run", "gtt_dry_run"]


def test_buy_with_zero_quantity_is_BLOCKED_before_the_gateway(gw) -> None:
    store = MemoryStore()
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, quantity=0)
    out = execute(store, gw, plan_id, line_id)
    assert out.status == "BLOCKED" and "0 shares" in out.reason
    assert out.order is None and out.gtt is None and out.position_id is None
    assert gw._sent == {} and store.positions == {}
    assert store.lines[line_id]["state"] == "REJECTED"
    assert store.sessions[NOW.date()]["confirms"] == 1
    assert store.sessions[NOW.date()]["fills"] == 0


def test_buy_with_stop_not_below_trigger_is_BLOCKED_before_the_gateway(gw) -> None:
    """`04` §6.1: a stop at or above the entry is an error, not a position."""
    store = MemoryStore()
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, stop=D("100.00"))
    out = execute(store, gw, plan_id, line_id)
    assert out.status == "BLOCKED" and "not below" in out.reason
    assert gw._sent == {} and store.positions == {}


def test_buy_for_a_name_already_held_is_BLOCKED(gw) -> None:
    """`04` §6.5: never averaged down."""
    store = MemoryStore()
    store.add_position(instrument_id=11)
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, instrument_id=11)
    out = execute(store, gw, plan_id, line_id)
    assert out.status == "BLOCKED" and "already held" in out.reason
    assert gw._sent == {}


def test_buy_untouchable_instrument_raises_and_marks_line_REJECTED(gw) -> None:
    """Non-negotiable 7: the guard raises before any network call; the line cannot be
    re-posted; the error is not swallowed."""
    from baskfy_execution.guards import UntouchableInstrumentError
    store = MemoryStore()
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, symbol="SGBAUG28")
    with pytest.raises(UntouchableInstrumentError):
        execute(store, gw, plan_id, line_id)
    assert store.lines[line_id]["state"] == "REJECTED"
    assert store.positions == {}


def test_buy_DUPLICATE_from_gateway_is_BLOCKED_with_no_second_position(gw) -> None:
    """A store that lost the book (two lines, one client id, and the first's position gone)
    still cannot double-send: the gateway's map answers DUPLICATE and the outcome is BLOCKED
    with the first order id. Re-pinned for SW10.4: with the position still on the book the
    confirm-time gate answers ALREADY_HELD before the gateway is asked, so the book is wiped
    between the two confirms to reach the gateway's own last line of defence."""
    store = MemoryStore()
    plan_id = store.add_plan()
    first = store.add_line(plan_id, instrument_id=11)
    second = store.add_line(plan_id, instrument_id=12)   # same symbol, different instrument
    out1 = execute(store, gw, plan_id, first)
    assert out1.status == "SIMULATED"
    store.positions.clear()   # the store lost the book; the line's FILLED state survives
    out2 = execute(store, gw, plan_id, second)
    assert out2.status == "BLOCKED" and "DUPLICATE" in out2.reason
    assert out2.order["order_id"] == out1.order["order_id"]
    assert store.positions == {} and len(store.fills) == 1, "no second position, no second fill"


def test_buy_risk_blocked_is_BLOCKED_with_the_gateway_error() -> None:
    from app.core.risk import RiskConfig
    gw = X.build_swing_gateway(ExplodingKC(), RiskManager(RiskConfig(max_position_value=1_000.0)))
    store = MemoryStore()
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id)
    out = execute(store, gw, plan_id, line_id)
    assert out.status == "BLOCKED" and "RISK_BLOCKED" in out.reason and "> cap" in out.reason
    assert store.positions == {} and store.lines[line_id]["state"] == "REJECTED"


def test_buy_gtt_refused_leaves_a_naked_position_and_says_so(gw) -> None:
    """The one way a simulated buy can end without a stop: the GTT id was already used (a
    store that reused a plan id). The shares are held, the position is NAKED, the reason
    says re-arm — never a silent success, never a phantom stop."""
    store = MemoryStore()
    plan_id = store.add_plan()
    gw._gtt_sent[f"{plan_id}:ALPHA:GTT"] = "DRY-earlier"
    line_id = store.add_line(plan_id)
    out = execute(store, gw, plan_id, line_id)
    assert out.status == "SIMULATED"
    assert out.gtt["status"] == "DUPLICATE"
    assert "NAKED" in out.reason and "re-arm" in out.reason
    pos = store.positions[out.position_id]
    assert pos["gtt_id"] is None and pos["quantity_open"] == 100


# --- G5: the flag, and the broker that is never reached -----------------------------------


def test_flag_off_dry_run_false_still_never_reaches_broker(monkeypatch, gw) -> None:
    """`02` Track B: with BASKFY_SWING_EXECUTION_ENABLED=false, the desk's DRY_RUN=false
    changes nothing — the same code path runs the gateway's dry-run branch."""
    monkeypatch.setattr(C, "DRY_RUN", False)
    monkeypatch.setattr(C, "SWING_EXECUTION_ENABLED", False)
    store = MemoryStore()
    plan_id = store.add_plan()
    buy = store.add_line(plan_id)
    out = execute(store, gw, plan_id, buy)
    assert out.status == "SIMULATED" and out.simulated is True
    assert out.order["status"] == "DRY_RUN" and out.gtt["status"] == DRY_RUN_GTT
    assert store.positions[out.position_id]["simulated"] is True
    assert store.fills[0]["simulated"] is True
    assert store.sessions[NOW.date()]["mode"] == "DRY_RUN"


def test_flag_off_sell_and_raise_never_reach_broker(monkeypatch, gw) -> None:
    monkeypatch.setattr(C, "DRY_RUN", False)
    store = MemoryStore()
    pid = store.add_position(gtt_id="DRY-old")
    plan_id = store.add_plan()
    sell = store.add_line(plan_id, kind="SELL_AT_OPEN", quantity=100, trigger=None, stop=None,
                          note="PARTIAL_INTO_STRENGTH")
    raise_ = store.add_line(plan_id, kind="RAISE_GTT_STOP", quantity=0, trigger=None,
                            stop=D("100.00"), note="BREAKEVEN_AFTER_PARTIAL")
    s = execute(store, gw, plan_id, sell, last_price=D("110"))
    r = execute(store, gw, plan_id, raise_, last_price=D("110"))
    assert s.status == "SIMULATED" and r.status == "SIMULATED"
    assert store.positions[pid]["stop"] == D("100.00")
    assert store.positions[pid]["gtt_id"].startswith("DRY-")


def test_dry_run_rearm_never_reaches_broker(gw) -> None:
    store = MemoryStore()
    pid = store.add_position(gtt_id=None, gtt_trigger=None, gtt_armed_at=None)
    out = run(X.rearm_gtt(store, gw, position_id=pid, confirm="true", now=NOW))
    assert out.status == "SIMULATED" and out.gtt["status"] == DRY_RUN_GTT
    assert store.positions[pid]["gtt_id"].startswith("DRY-")


def test_never_reaches_broker_across_the_whole_module_is_the_kc_contract() -> None:
    """The fake is the proof: every method that could reach Zerodha raises."""
    kc = ExplodingKC()
    for name in ("place_order", "place_gtt", "delete_gtt", "instruments"):
        with pytest.raises(AssertionError):
            getattr(kc, name)()


# --- G3 / G4: SELL_AT_OPEN ----------------------------------------------------------------


def test_sell_for_more_than_quantity_open_is_BLOCKED(gw) -> None:
    store = MemoryStore()
    pid = store.add_position(quantity_entered=300, quantity_open=200, state="PARTIAL")
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, kind="SELL_AT_OPEN", quantity=201, trigger=None, stop=None)
    out = execute(store, gw, plan_id, line_id, last_price=D("105"))
    assert out.status == "BLOCKED" and "exceeds the 200 open" in out.reason
    assert out.position_id == pid
    assert gw._sent == {} and store.fills == []
    assert store.positions[pid]["quantity_open"] == 200


def test_sell_for_a_symbol_not_in_sw_position_is_BLOCKED(gw) -> None:
    """Track C §5: the sleeve never sells a holding it did not buy — and it never asks the
    broker's holdings, which contain the weekly book."""
    store = MemoryStore()
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, kind="SELL_AT_OPEN", symbol="WEEKLYNAME",
                             instrument_id=77, quantity=10, trigger=None, stop=None)
    out = execute(store, gw, plan_id, line_id, last_price=D("50"))
    assert out.status == "BLOCKED" and out.reason == "WEEKLYNAME: not a swing position"
    assert gw._sent == {}


def test_sell_when_quantity_open_already_zero_is_BLOCKED(gw) -> None:
    store = MemoryStore()
    store.add_position(quantity_open=0, state="CLOSED")
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, kind="SELL_AT_OPEN", quantity=1, trigger=None, stop=None)
    out = execute(store, gw, plan_id, line_id, last_price=D("50"))
    assert out.status == "BLOCKED" and "not a swing position" in out.reason


def test_sell_with_zero_quantity_is_BLOCKED(gw) -> None:
    store = MemoryStore()
    store.add_position()
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, kind="SELL_AT_OPEN", quantity=0, trigger=None, stop=None)
    out = execute(store, gw, plan_id, line_id, last_price=D("50"))
    assert out.status == "BLOCKED" and "0 shares" in out.reason
    assert gw._sent == {}


def test_sell_without_last_price_is_BLOCKED_not_guessed(gw) -> None:
    store = MemoryStore()
    store.add_position()
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, kind="SELL_AT_OPEN", quantity=100, trigger=None, stop=None)
    out = execute(store, gw, plan_id, line_id)
    assert out.status == "BLOCKED" and "last_price" in out.reason
    assert gw._sent == {} and store.fills == []


def test_sell_partial_resizes_gtt_for_the_remainder(gw) -> None:
    """`04` §6.3/§6.4: the third comes off, and the stop that covered 300 now covers 200 —
    delete then place, in the same call."""
    store = MemoryStore()
    pid = store.add_position(gtt_id="DRY-buy", quantity_entered=300, quantity_open=300)
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, kind="SELL_AT_OPEN", quantity=100, trigger=None, stop=None,
                             note="PARTIAL_INTO_STRENGTH")
    out = execute(store, gw, plan_id, line_id, last_price=D("110.00"))
    assert out.status == "SIMULATED" and out.reason == ""
    assert out.order["status"] == "DRY_RUN"
    assert out.gtt["status"] == DRY_RUN_GTT and out.gtt["qty"] == 200
    assert out.gtt["trigger"] == 96.0
    pos = store.positions[pid]
    assert pos["quantity_open"] == 200 and pos["state"] == "PARTIAL"
    assert pos["partial_done"] is True and pos["partial_date"] == NOW.date()
    assert pos["exit_avg"] == D("110.00")
    assert pos["gtt_id"] == f"DRY-{plan_id}:ALPHA:SELL:GTT" and pos["gtt_trigger"] == D("96.00")
    assert pos["closed_on"] is None and pos["r_multiple"] is None
    assert store.fills[-1]["side"] == "SELL" and store.fills[-1]["quantity"] == 100
    assert store.fills[-1]["price"] == D("110.00") and store.fills[-1]["simulated"] is True
    assert store.lines[line_id]["state"] == "FILLED"
    assert store.sessions[NOW.date()]["fills"] == 1


def test_sell_partial_gtt_resize_goes_through_gateway_delete_for_a_real_id(gw) -> None:
    """A real trigger id is cancelled through the gateway (dry-run → DRY_RUN_GTT_DELETE); a
    simulated one is not handed to it, because there is nothing at the exchange."""
    store = MemoryStore()
    pid = store.add_position(gtt_id="123456")
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, kind="SELL_AT_OPEN", quantity=100, trigger=None, stop=None)
    out = execute(store, gw, plan_id, line_id, last_price=D("105"))
    assert out.status == "SIMULATED"
    events = journal_events(gw)
    assert events == ["dry_run", "gtt_dry_run_delete", "gtt_dry_run"]
    assert store.positions[pid]["gtt_id"].startswith("DRY-")


def test_sell_and_raise_for_the_same_name_in_one_plan_both_arm(gw) -> None:
    """SW5's evening fixture produces exactly this pair — a third off AND a stop to breakeven —
    for one name in one plan. Their GTT ids must differ or the second is DUPLICATE, and a
    DUPLICATE after a cancel is a naked position."""
    store = MemoryStore()
    pid = store.add_position(gtt_id="DRY-buy", quantity_entered=300, quantity_open=300)
    plan_id = store.add_plan()
    sell = store.add_line(plan_id, kind="SELL_AT_OPEN", quantity=100, trigger=None, stop=None,
                          note="PARTIAL_INTO_STRENGTH")
    raise_ = store.add_line(plan_id, kind="RAISE_GTT_STOP", quantity=0, trigger=None,
                            stop=D("100.00"), note="BREAKEVEN_AFTER_PARTIAL")
    s = execute(store, gw, plan_id, sell, last_price=D("104"))
    r = execute(store, gw, plan_id, raise_, last_price=D("104"))
    assert s.status == "SIMULATED" and s.gtt["qty"] == 200 and s.gtt["trigger"] == 96.0
    assert r.status == "SIMULATED" and r.gtt["qty"] == 200 and r.gtt["trigger"] == 100.0
    pos = store.positions[pid]
    assert pos["quantity_open"] == 200 and pos["stop"] == D("100.00")
    assert pos["gtt_id"] == f"DRY-{plan_id}:ALPHA:RAISE:GTT"


def test_sell_close_out_writes_r_multiple_pnl_exit_avg_close_reason(gw) -> None:
    """`04` §10: r = (exit_avg − entry) / (entry − initial_stop), 2 dp; pnl = (exit_avg −
    entry) × quantity; the GTT is gone and the position is CLOSED."""
    store = MemoryStore()
    pid = store.add_position(gtt_id="DRY-buy", quantity_entered=300, quantity_open=300,
                             entry_avg=D("100.00"), initial_stop=D("96.00"), stop=D("100.00"))
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, kind="SELL_AT_OPEN", quantity=300, trigger=None, stop=None,
                             note="CLOSE_BELOW_TRAIL_MA")
    out = execute(store, gw, plan_id, line_id, last_price=D("109.00"))
    assert out.status == "SIMULATED" and out.reason == ""
    assert out.gtt["status"] == DRY_RUN_GTT_DELETE
    pos = store.positions[pid]
    assert pos["state"] == "CLOSED" and pos["quantity_open"] == 0
    assert pos["closed_on"] == NOW.date()
    assert pos["exit_avg"] == D("109.00")
    assert pos["close_reason"] == "CLOSE_BELOW_TRAIL_MA"
    assert pos["r_multiple"] == D("2.25")
    assert pos["pnl_inr"] == D("2700.00")
    assert pos["gtt_id"] is None and pos["gtt_trigger"] is None and pos["gtt_armed_at"] is None
    assert pos["r_multiple"] == r_multiple(entry=D("100"), stop=D("96"), exit_price=D("109"))
    closed = ClosedTrade(symbol="ALPHA", setup="FLAG", entry_date=NOW.date(),
                         exit_date=NOW.date(), entry=D("100"), initial_stop=D("96"),
                         exit_avg=D("109"), quantity=300)
    assert pos["pnl_inr"] == closed.pnl_inr and pos["r_multiple"] == closed.r_multiple


def test_close_after_partial_exit_avg_is_share_weighted(gw) -> None:
    """100 sold at 110, then 200 at 104: exit_avg = (100×110 + 200×104)/300 = 106.00,
    R = 6/4 = 1.50, pnl = 6 × 300 = 1,800."""
    store = MemoryStore()
    pid = store.add_position(gtt_id="DRY-buy", quantity_entered=300, quantity_open=300)
    plan_id = store.add_plan()
    partial = store.add_line(plan_id, kind="SELL_AT_OPEN", quantity=100, trigger=None,
                             stop=None, note="PARTIAL_INTO_STRENGTH")
    execute(store, gw, plan_id, partial, last_price=D("110.00"))
    plan2 = store.add_plan()
    rest = store.add_line(plan2, kind="SELL_AT_OPEN", quantity=200, trigger=None, stop=None,
                          note="CLOSE_BELOW_TRAIL_MA")
    out = execute(store, gw, plan2, rest, last_price=D("104.00"))
    assert out.status == "SIMULATED"
    pos = store.positions[pid]
    assert pos["exit_avg"] == D("106.00")
    assert pos["r_multiple"] == D("1.50")
    assert pos["pnl_inr"] == D("1800.00")
    assert pos["state"] == "CLOSED" and pos["close_reason"] == "CLOSE_BELOW_TRAIL_MA"
    assert [f["side"] for f in store.fills] == ["SELL", "SELL"]


def test_close_with_unknown_note_is_MANUAL_and_a_loss_has_negative_r(gw) -> None:
    store = MemoryStore()
    pid = store.add_position(gtt_id=None, quantity_entered=100, quantity_open=100,
                             entry_avg=D("100.00"), initial_stop=D("96.00"))
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, kind="SELL_AT_OPEN", quantity=100, trigger=None, stop=None,
                             note="operator clicked sell")
    out = execute(store, gw, plan_id, line_id, last_price=D("97.00"))
    assert out.status == "SIMULATED" and out.gtt is None   # nothing was resting to cancel
    pos = store.positions[pid]
    assert pos["close_reason"] == "MANUAL"
    assert pos["r_multiple"] == D("-0.75") and pos["pnl_inr"] == D("-300.00")


def test_sell_partial_on_a_naked_position_arms_a_gtt_for_the_remainder(gw) -> None:
    store = MemoryStore()
    pid = store.add_position(gtt_id=None, gtt_trigger=None, gtt_armed_at=None)
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, kind="SELL_AT_OPEN", quantity=100, trigger=None, stop=None)
    out = execute(store, gw, plan_id, line_id, last_price=D("110"))
    assert out.status == "SIMULATED" and out.gtt["status"] == DRY_RUN_GTT and out.gtt["qty"] == 200
    assert store.positions[pid]["gtt_id"] is not None


def test_sell_partial_when_price_is_below_stop_leaves_naked_with_reason(gw) -> None:
    """The remainder's GTT is refused (trigger ≥ last price is not a stop); the sale still
    happened, the position is NAKED and the reason says so."""
    store = MemoryStore()
    pid = store.add_position(gtt_id="DRY-buy", stop=D("96.00"))
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, kind="SELL_AT_OPEN", quantity=100, trigger=None, stop=None)
    out = execute(store, gw, plan_id, line_id, last_price=D("95.00"))
    assert out.status == "SIMULATED" and "NAKED" in out.reason
    assert out.gtt["status"] == "BLOCKED"
    pos = store.positions[pid]
    assert pos["quantity_open"] == 200 and pos["gtt_id"] is None


# --- G3 / G4: RAISE_GTT_STOP --------------------------------------------------------------


def test_raise_below_resting_stop_is_BLOCKED(gw) -> None:
    """`04` §6.5: a stop never falls."""
    store = MemoryStore()
    pid = store.add_position(stop=D("98.00"), gtt_id="DRY-buy")
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, kind="RAISE_GTT_STOP", quantity=0, trigger=None,
                             stop=D("97.00"))
    out = execute(store, gw, plan_id, line_id, last_price=D("110"))
    assert out.status == "BLOCKED" and "never falls" in out.reason
    assert store.positions[pid]["stop"] == D("98.00")
    assert store.positions[pid]["gtt_id"] == "DRY-buy"
    assert gw._gtt_sent == {}


def test_raise_equal_to_resting_stop_is_BLOCKED(gw) -> None:
    store = MemoryStore()
    store.add_position(stop=D("98.00"))
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, kind="RAISE_GTT_STOP", quantity=0, trigger=None,
                             stop=D("98.00"))
    out = execute(store, gw, plan_id, line_id, last_price=D("110"))
    assert out.status == "BLOCKED"


def test_raise_for_symbol_not_held_is_BLOCKED(gw) -> None:
    store = MemoryStore()
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, kind="RAISE_GTT_STOP", quantity=0, trigger=None,
                             stop=D("98.00"))
    out = execute(store, gw, plan_id, line_id, last_price=D("110"))
    assert out.status == "BLOCKED" and "not a swing position" in out.reason


def test_raise_without_last_price_is_BLOCKED(gw) -> None:
    store = MemoryStore()
    store.add_position()
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, kind="RAISE_GTT_STOP", quantity=0, trigger=None,
                             stop=D("100.00"))
    out = execute(store, gw, plan_id, line_id)
    assert out.status == "BLOCKED" and "last_price" in out.reason


def test_raise_at_or_above_last_price_is_BLOCKED_before_cancelling(gw) -> None:
    """Refused HERE, not by the gateway: the gateway's refusal comes after the cancel, which
    would have left the position naked."""
    store = MemoryStore()
    pid = store.add_position(gtt_id="DRY-buy")
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, kind="RAISE_GTT_STOP", quantity=0, trigger=None,
                             stop=D("100.00"))
    out = execute(store, gw, plan_id, line_id, last_price=D("100.00"))
    assert out.status == "BLOCKED" and "fire at once" in out.reason
    assert store.positions[pid]["gtt_id"] == "DRY-buy"


def test_raise_gtt_stop_replaces_the_trigger_and_updates_the_position(gw) -> None:
    store = MemoryStore()
    pid = store.add_position(gtt_id="123", gtt_trigger=D("96.00"), stop=D("96.00"))
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, kind="RAISE_GTT_STOP", quantity=0, trigger=None,
                             stop=D("100.00"), note="BREAKEVEN_AT_R")
    out = execute(store, gw, plan_id, line_id, last_price=D("104.00"))
    assert out.status == "SIMULATED" and out.reason == ""
    assert out.order is None and out.gtt["status"] == DRY_RUN_GTT and out.gtt["qty"] == 300
    pos = store.positions[pid]
    assert pos["stop"] == D("100.00") and pos["gtt_trigger"] == D("100.00")
    assert pos["gtt_id"] == f"DRY-{plan_id}:ALPHA:RAISE:GTT" and pos["gtt_armed_at"] == NOW
    assert pos["initial_stop"] == D("96.00")     # the R denominator never moves
    events = journal_events(gw)
    assert events == ["gtt_dry_run_delete", "gtt_dry_run"]
    assert store.lines[line_id]["state"] == "FILLED"
    assert store.sessions[NOW.date()] == {"mode": "DRY_RUN", "confirms": 1, "fills": 0,
                                          "manage_actions": 1}


def test_raise_when_cancel_is_refused_places_no_second_gtt(gw) -> None:
    """Two triggers sell the position twice: if the old one cannot be pulled the new one is
    not armed, the stop stays, the reason is the gateway's."""
    from baskfy_execution.risk import RiskConfig
    risk = RiskManager(RiskConfig(max_daily_loss=10.0))
    risk.on_pnl(-100.0)          # trips the kill switch: delete_gtt refuses, arming would not
    gw = X.build_swing_gateway(ExplodingKC(), risk)
    store = MemoryStore()
    pid = store.add_position(gtt_id="123", stop=D("96.00"))
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, kind="RAISE_GTT_STOP", quantity=0, trigger=None,
                             stop=D("100.00"))
    out = execute(store, gw, plan_id, line_id, last_price=D("104"))
    assert out.status == "BLOCKED" and "KILL SWITCH" in out.reason
    assert gw._gtt_sent == {}
    assert store.positions[pid]["stop"] == D("96.00") and store.positions[pid]["gtt_id"] == "123"


# --- rearm_gtt ---------------------------------------------------------------------------


def test_rearm_gtt_400_without_confirm(gw) -> None:
    store = MemoryStore()
    pid = store.add_position(gtt_id=None)
    with pytest.raises(HTTPException) as err:
        run(X.rearm_gtt(store, gw, position_id=pid, confirm="false", now=NOW))
    assert err.value.status_code == 400


def test_rearm_gtt_404_unknown_position(gw) -> None:
    with pytest.raises(HTTPException) as err:
        run(X.rearm_gtt(MemoryStore(), gw, position_id=9, confirm="true", now=NOW))
    assert err.value.status_code == 404


def test_rearm_gtt_arms_naked_position_for_quantity_open(gw) -> None:
    store = MemoryStore()
    pid = store.add_position(gtt_id=None, gtt_trigger=None, gtt_armed_at=None,
                             quantity_entered=300, quantity_open=200, state="PARTIAL",
                             stop=D("97.00"))
    out = run(X.rearm_gtt(store, gw, position_id=pid, confirm="true", now=NOW))
    assert out.status == "SIMULATED" and out.position_id == pid and out.order is None
    assert out.gtt["status"] == DRY_RUN_GTT and out.gtt["qty"] == 200 and out.gtt["trigger"] == 97.0
    pos = store.positions[pid]
    assert pos["gtt_id"].startswith("DRY-") and pos["gtt_trigger"] == D("97.00")
    assert pos["gtt_armed_at"] == NOW and pos["stop"] == D("97.00")
    assert store.sessions[NOW.date()]["manage_actions"] == 1


def test_rearm_gtt_on_a_covered_position_is_BLOCKED(gw) -> None:
    store = MemoryStore()
    pid = store.add_position(gtt_id="DRY-buy")
    out = run(X.rearm_gtt(store, gw, position_id=pid, confirm="true", now=NOW))
    assert out.status == "BLOCKED" and "already resting" in out.reason
    assert gw._gtt_sent == {}


def test_rearm_gtt_on_a_closed_position_is_BLOCKED(gw) -> None:
    store = MemoryStore()
    pid = store.add_position(gtt_id=None, quantity_open=0, state="CLOSED")
    out = run(X.rearm_gtt(store, gw, position_id=pid, confirm="true", now=NOW))
    assert out.status == "BLOCKED" and "nothing is open" in out.reason


def test_rearm_gtt_breakeven_stop_needs_a_last_price(gw) -> None:
    """A stop raised to the entry cannot be checked against the entry."""
    store = MemoryStore()
    pid = store.add_position(gtt_id=None, stop=D("100.00"), entry_avg=D("100.00"))
    out = run(X.rearm_gtt(store, gw, position_id=pid, confirm="true", now=NOW))
    assert out.status == "BLOCKED" and "last_price" in out.reason
    out = run(X.rearm_gtt(store, gw, position_id=pid, confirm="true", now=NOW,
                          last_price=D("103")))
    assert out.status == "SIMULATED"


# --- G6: first_live_sessions_left (SW7.2) --------------------------------------------------


class RecordingGateway:
    """Stands in for a LIVE gateway on the one path no test may run through a real one:
    it records the quantity asked for and answers as the broker would have."""

    def __init__(self) -> None:
        self.orders: list[dict] = []
        self.gtts: list[dict] = []

    async def place(self, **kw):
        self.orders.append(kw)
        return {"symbol": kw["symbol"], "status": "PLACED", "order_id": f"ORD{len(self.orders)}"}

    async def place_gtt_stop(self, **kw):
        self.gtts.append(kw)
        return {"symbol": kw["symbol"], "status": "GTT_PLACED", "gtt_id": 4242,
                "trigger": kw["trigger"], "limit": kw["trigger"] * 0.995}

    async def delete_gtt(self, **kw):
        return {"symbol": kw["symbol"], "gtt_id": kw["gtt_id"], "status": "GTT_DELETED"}


@pytest.fixture()
def live(monkeypatch):
    """The gates a real session would run under. NO real gateway is built under this fixture."""
    monkeypatch.setattr(C, "DRY_RUN", False)
    monkeypatch.setattr(C, "SWING_EXECUTION_ENABLED", True)
    assert X.swing_gates().dry_run is False
    return RecordingGateway()


def test_first_live_quantity_rule() -> None:
    assert X.first_live_quantity(100, sessions_left=5, simulated=False) == 50
    assert X.first_live_quantity(101, sessions_left=1, simulated=False) == 50   # round down
    assert X.first_live_quantity(1, sessions_left=5, simulated=False) == 1      # min 1
    assert X.first_live_quantity(2, sessions_left=5, simulated=False) == 1
    assert X.first_live_quantity(100, sessions_left=0, simulated=False) == 100
    assert X.first_live_quantity(100, sessions_left=5, simulated=True) == 100


def test_first_live_halves_quantity_only_when_not_simulated(gw) -> None:
    """Simulated: the paper record is at full size, and the counter does not move."""
    store = MemoryStore(first_live_sessions_left=5)
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, quantity=100)
    out = execute(store, gw, plan_id, line_id)
    assert out.simulated is True
    assert store.positions[out.position_id]["quantity_entered"] == 100
    assert store.first_live_writes == []
    assert store.config()["first_live_sessions_left"] == 5


def test_first_live_halves_a_live_buy_and_counts_down_once_per_session(live) -> None:
    store = MemoryStore(first_live_sessions_left=5)
    plan_id = store.add_plan()
    a = store.add_line(plan_id, quantity=100, symbol="ALPHA", instrument_id=11)
    b = store.add_line(plan_id, quantity=51, symbol="BETA", instrument_id=12)
    out_a = execute(store, live, plan_id, a)
    out_b = execute(store, live, plan_id, b)
    assert out_a.status == out_b.status == "SENT"
    assert [o["qty"] for o in live.orders] == [50, 25]
    assert store.first_live_writes == [4], "decremented once for the session, not per order"
    next_day = NOW + dt.timedelta(days=1)
    plan2 = store.add_plan(built_at=next_day)
    c = store.add_line(plan2, quantity=100, symbol="GAMMA", instrument_id=13)
    execute(store, live, plan2, c, now=next_day)
    assert store.first_live_writes == [4, 3]


def test_first_live_at_zero_sends_full_size(live) -> None:
    store = MemoryStore(first_live_sessions_left=0)
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, quantity=100)
    execute(store, live, plan_id, line_id)
    assert live.orders[0]["qty"] == 100 and store.first_live_writes == []


def test_live_buy_not_yet_filled_is_SENT_with_no_position_and_no_gtt(live) -> None:
    """SW7.1: a PLACED live order is not a fill — no position, no stop, line SENT with the
    order id, so the fill can be reconciled later and the stop armed for what filled."""
    store = MemoryStore()
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id)
    out = execute(store, live, plan_id, line_id)
    assert out.status == "SENT" and out.simulated is False and out.position_id is None
    assert out.gtt is None and live.gtts == []
    assert store.positions == {} and store.fills == []
    assert store.lines[line_id]["state"] == "SENT"
    assert store.lines[line_id]["journal_ref"] == "ORD1"
    assert store.sessions[NOW.date()] == {"mode": "LIVE", "confirms": 1, "fills": 0,
                                          "manage_actions": 0}
    order = live.orders[0]
    assert order["side"] == "BUY" and order["product"] == "CNC" and order["order_type"] == "LIMIT"
    assert order["price"] == 100.0 and order["client_id"] == f"{plan_id}:ALPHA:BUY"
    assert order["tenant"].user_id == C.SOLE_USER_ID


def test_live_sell_not_yet_filled_is_SENT_and_book_unchanged(live) -> None:
    store = MemoryStore()
    pid = store.add_position(gtt_id="123")
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, kind="SELL_AT_OPEN", quantity=100, trigger=None, stop=None)
    out = execute(store, live, plan_id, line_id)
    assert out.status == "SENT" and out.position_id == pid
    assert store.positions[pid]["quantity_open"] == 300 and store.positions[pid]["gtt_id"] == "123"
    assert store.fills == []
    assert live.orders[0]["order_type"] == "MARKET" and live.orders[0]["side"] == "SELL"


def test_live_raise_records_the_exchange_gtt_id(live) -> None:
    store = MemoryStore()
    pid = store.add_position(gtt_id="123", stop=D("96.00"))
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, kind="RAISE_GTT_STOP", quantity=0, trigger=None,
                             stop=D("100.00"))
    out = execute(store, live, plan_id, line_id, last_price=D("104"))
    assert out.status == "FILLED" and out.simulated is False
    pos = store.positions[pid]
    assert pos["gtt_id"] == "4242" and pos["stop"] == D("100.00")
    assert live.gtts[0]["qty"] == 300 and live.gtts[0]["trigger"] == 100.0


# --- G8: the code names no broker method ---------------------------------------------------


def _code_only(source: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.body and isinstance(node.body[0], ast.Expr) and isinstance(
                getattr(node.body[0], "value", None), ast.Constant
            ):
                node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def test_module_code_never_names_a_broker_method() -> None:
    code = _code_only(inspect.getsource(X))
    for banned in ("kc.place_order", "kc.place_gtt", "kc.delete_gtt", "kiteconnect",
                   "kite_client", "kc.instruments"):
        assert banned not in code, banned
    assert "gw.place(" in code and "gw.place_gtt_stop(" in code and "gw.delete_gtt(" in code


def test_prices_are_float_at_the_gateway_boundary(live) -> None:
    store = MemoryStore()
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, trigger=D("123.45"), stop=D("119.90"))
    execute(store, live, plan_id, line_id)
    assert isinstance(live.orders[0]["price"], float) and live.orders[0]["price"] == 123.45


def test_prices_stay_decimal_in_the_store(gw) -> None:
    store = MemoryStore()
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, trigger=D("123.45"), stop=D("119.90"))
    out = execute(store, gw, plan_id, line_id)
    pos = store.positions[out.position_id]
    for key in ("entry_avg", "initial_stop", "stop", "gtt_trigger"):
        assert isinstance(pos[key], Decimal), key
    assert pos["gtt_trigger"] == D("119.90")
    assert isinstance(store.fills[0]["price"], Decimal)


def test_live_gates_with_an_exploding_kc_are_REJECTED_never_a_position(monkeypatch) -> None:
    """The other direction of the proof: if a real gateway under LIVE gates ever met this
    module's fake broker, the gateway's ERROR taxonomy would surface as REJECTED and nothing
    would be written — no position, no fill, no stop."""
    monkeypatch.setattr(C, "DRY_RUN", False)
    monkeypatch.setattr(C, "SWING_EXECUTION_ENABLED", True)
    gw = X.build_swing_gateway(ExplodingKC(), RiskManager())
    store = MemoryStore()
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id)
    out = execute(store, gw, plan_id, line_id)
    assert out.status == "REJECTED" and out.order["status"] == "ERROR"
    assert "reached the broker" in out.reason
    assert store.positions == {} and store.fills == []
    assert store.lines[line_id]["state"] == "REJECTED"


# --- edge cases hunted on the third pass ----------------------------------------------------


def test_gtt_outside_the_swing_band_is_journalled_not_refused(gw) -> None:
    """PACK.3 / `StopBand`: a stop outside 0.5–10 % is a finding for a person, never a
    refusal that leaves the position naked. Re-pinned for SW9.5/SW10.4: a stop 12 % below the
    entry is `STOP_TOO_WIDE` at the confirm-time gate (`04` §6.1, one ADR or tighter) and never
    reaches a GTT, so the band's *near* edge is what is left to test — a stop 0.3 % under the
    entry is inside one ADR, outside the band's 0.5 % floor, and journalled, not refused."""
    store = MemoryStore()
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, trigger=D("100.00"), stop=D("99.70"))
    out = execute(store, gw, plan_id, line_id)
    assert out.status == "SIMULATED" and out.gtt["status"] == DRY_RUN_GTT
    assert store.positions[out.position_id]["gtt_id"] is not None
    assert "gtt_band_warning" in journal_events(gw)


def test_naive_timestamps_are_read_as_ist(gw) -> None:
    store = MemoryStore()
    plan_id = store.add_plan(built_at=NOW.replace(tzinfo=None) - dt.timedelta(minutes=5))
    line_id = store.add_line(plan_id)
    out = execute(store, gw, plan_id, line_id, now=NOW.replace(tzinfo=None))
    assert out.status == "SIMULATED"
    assert store.fills[0]["filled_at"].tzinfo is not None
    assert store.positions[out.position_id]["entry_date"] == NOW.date()
    assert list(store.sessions) == [NOW.date()]


def test_session_day_is_the_ist_date(gw) -> None:
    """A confirm at 09:31 IST is 04:01 UTC the same day; one at 00:30 IST is yesterday UTC.
    The session row is keyed by the trading day, which is IST's."""
    store = MemoryStore()
    late = dt.datetime(2026, 9, 3, 0, 30, tzinfo=IST)
    plan_id = store.add_plan(built_at=late - dt.timedelta(minutes=1))
    line_id = store.add_line(plan_id)
    execute(store, gw, plan_id, line_id, now=late.astimezone(dt.UTC))
    assert list(store.sessions) == [dt.date(2026, 9, 3)]


def test_line_with_no_quantity_is_BLOCKED(gw) -> None:
    store = MemoryStore()
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, quantity=None)
    out = execute(store, gw, plan_id, line_id)
    assert out.status == "BLOCKED" and gw._sent == {}


def test_unknown_kind_is_BLOCKED_and_sends_nothing(gw) -> None:
    store = MemoryStore()
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, kind="SHORT_ON_TRIGGER")
    out = execute(store, gw, plan_id, line_id)
    assert out.status == "BLOCKED" and gw._sent == {} and gw._gtt_sent == {}
    assert store.lines[line_id]["state"] == "REJECTED"


def test_rearm_twice_on_one_day_is_DUPLICATE_BLOCKED(gw) -> None:
    """The gateway's own GTT map: a second arm under the same id is DUPLICATE, so even a store
    that forgot the first gtt_id cannot get two triggers resting."""
    store = MemoryStore()
    pid = store.add_position(gtt_id=None)
    first = run(X.rearm_gtt(store, gw, position_id=pid, confirm="true", now=NOW))
    assert first.status == "SIMULATED"
    store.update_position(pid, {"gtt_id": None})     # a store that lost the id
    second = run(X.rearm_gtt(store, gw, position_id=pid, confirm="true", now=NOW))
    assert second.status == "BLOCKED" and "DUPLICATE" in second.reason


def test_two_partials_keep_a_running_share_weighted_exit_avg(gw) -> None:
    """100 at 110 then 100 at 120 with 100 still open: exit_avg 115.00, still PARTIAL."""
    store = MemoryStore()
    pid = store.add_position(gtt_id="DRY-buy", quantity_entered=300, quantity_open=300)
    for price in ("110.00", "120.00"):
        plan_id = store.add_plan()
        line_id = store.add_line(plan_id, kind="SELL_AT_OPEN", quantity=100, trigger=None,
                                 stop=None, note="PARTIAL_INTO_STRENGTH")
        assert execute(store, gw, plan_id, line_id, last_price=D(price)).status == "SIMULATED"
    pos = store.positions[pid]
    assert pos["exit_avg"] == D("115.00") and pos["quantity_open"] == 100
    assert pos["state"] == "PARTIAL" and pos["r_multiple"] is None


# --- SW9.5: the swing GTT rests 3 % under its trigger (docs/swing/04 §9.4, PACK.8) -----------


def test_the_swing_gtt_limit_fraction_is_a_config_constant_read_from_env(monkeypatch) -> None:
    """`BASKFY_SWING_GTT_LIMIT_FRACTION`, default 0.97: he uses market stops, a GTT fires a
    LIMIT, and a half-percent cushion (the gateway's own 0.995, the weekly book's) can be walked
    through by a fast-falling book. Read at import like the other swing knobs, never a form."""
    assert C.SWING_GTT_LIMIT_FRACTION == 0.97
    assert 0.0 < C.SWING_GTT_LIMIT_FRACTION < 0.995, "tighter than the weekly book's cushion"
    src = inspect.getsource(C)
    assert 'os.getenv("BASKFY_SWING_GTT_LIMIT_FRACTION", "0.97")' in src


def test_every_live_swing_gtt_is_armed_with_the_swing_limit_fraction(live) -> None:
    """Every `place_gtt_stop` the swing route makes carries `limit_fraction=0.97`, and no call
    leaves it to the gateway's default — the one way the weekly book's cushion could leak into
    a swing stop. Under LIVE gates a buy and a sell are SENT and arm nothing yet (SW7.1), so
    the recording gateway sees the raise and the re-arm."""
    store = MemoryStore(first_live_sessions_left=0)
    pid = store.add_position(gtt_id="123", stop=D("96.00"))
    plan_id = store.add_plan()
    raise_ = store.add_line(plan_id, kind="RAISE_GTT_STOP", quantity=0, trigger=None,
                            stop=D("100.00"))
    assert execute(store, live, plan_id, raise_, last_price=D("104")).status == "FILLED"
    store.update_position(pid, {"gtt_id": None})
    assert run(X.rearm_gtt(store, live, position_id=pid, confirm="true", now=NOW,
                           last_price=D("104"))).status == "FILLED"
    assert len(live.gtts) == 2, "the raise and the re-arm"
    assert [g["limit_fraction"] for g in live.gtts] == [0.97, 0.97]
    assert {g["limit_fraction"] for g in live.gtts} == {C.SWING_GTT_LIMIT_FRACTION}


def test_every_dry_run_swing_gtt_journals_the_swing_limit_fraction(gw) -> None:
    """Through the REAL gateway in its dry-run branch: a buy, a partial sell, a raise and a
    re-arm are four journalled GTTs, each saying which cushion the live trigger would carry —
    so the paper sessions of `docs/swing/02` §3.2 rehearse the number too."""
    store = MemoryStore()
    plan_id = store.add_plan()
    buy = store.add_line(plan_id, quantity=100, symbol="ALPHA", instrument_id=11)
    assert execute(store, gw, plan_id, buy).status == "SIMULATED"
    pid = store.add_position(gtt_id="DRY-old", quantity_entered=300, quantity_open=300,
                             symbol="BETA", instrument_id=12)
    sell = store.add_line(plan_id, kind="SELL_AT_OPEN", quantity=100, trigger=None, stop=None,
                          note="PARTIAL_INTO_STRENGTH", symbol="BETA", instrument_id=12)
    assert execute(store, gw, plan_id, sell, last_price=D("110")).status == "SIMULATED"
    raise_ = store.add_line(plan_id, kind="RAISE_GTT_STOP", quantity=0, trigger=None,
                            stop=D("100.00"), symbol="BETA", instrument_id=12)
    assert execute(store, gw, plan_id, raise_, last_price=D("110")).status == "SIMULATED"
    store.update_position(pid, {"gtt_id": None})
    assert run(X.rearm_gtt(store, gw, position_id=pid, confirm="true", now=NOW,
                           last_price=D("110"))).status == "SIMULATED"
    rows = [json.loads(row) for row in pathlib.Path(gw._journal_path).read_text().splitlines()]
    armed = [row for row in rows if row["event"] == "gtt_dry_run"]
    assert len(armed) == 4, "buy, partial re-arm, raise, re-arm"
    assert {row["limit_fraction"] for row in armed} == {0.97}


def test_the_weekly_books_gtt_path_does_not_name_the_swing_cushion() -> None:
    """The constant is passed per call by the swing route only. Nothing in the weekly book's
    order path (`app/main.py`, `app/kite_client.py`, `app/protection.py`) names it, so the
    weekly book's GTTs are byte-for-byte what they were."""
    for name in ("app/main.py", "app/kite_client.py", "app/protection.py"):
        path = pathlib.Path(__file__).resolve().parents[1] / name
        if path.exists():
            assert "SWING_GTT_LIMIT_FRACTION" not in path.read_text(), name
            assert "limit_fraction" not in path.read_text(), name


# --- SW10.4 (STANDING-ANSWERS A5): the confirm-time gate ------------------------------------
#
# A plan line's size is a preview; the confirm is the gate. Under the day's session lock the
# book is re-derived and the BUY re-sized through `build_entries` (via `entries_now`) against
# the rung's ceiling, the position count and the per-session cap. Every case below is `04`
# §5.3 / §8.4 / §9.1 restated at the moment of the click, over the real dry-run gateway.


def _rung0_book(store: MemoryStore) -> None:
    """ALPHA held: 1,666 × 100.80 = ₹1,67,932.80, 16.79 % of the ₹10 lakh sleeve (the drill's
    first confirm, `docs/swing/STATUS.md` SW10)."""
    store.add_position(symbol="ALPHA", instrument_id=11, entry_avg=D("100.80"),
                       quantity_entered=1666, quantity_open=1666, initial_stop=D("97.80"),
                       stop=D("97.80"))


def _beta_line(store: MemoryStore, plan_id: str, quantity: int = 833) -> int:
    """The drill's second trigger: BETA 833 × 210 = ₹1,74,930 — fits a 25 % rung alone, not
    beside ALPHA (together 34.29 %)."""
    store.detected["BETA"] = (D("6.00"), D(200_000_000), D("70.00"))
    return store.add_line(plan_id, symbol="BETA", instrument_id=12, setup="EP",
                          quantity=quantity, trigger=D("210.00"), stop=D("204.00"),
                          risk_inr=D("4998.00"), position_value=D("174930.00"))


def test_confirm_re_sizes_a_line_to_the_rungs_ceiling_and_rewrites_the_row(gw) -> None:
    """A5, the drill's arithmetic: rung 0 is 25 % of ₹10 lakh = ₹2,50,000; ALPHA holds
    ₹1,67,932.80; BETA's 833 shares would make 34.29 %. The confirm sends the 390 shares that
    fit the ₹82,067.20 of headroom (390 × 210 = ₹81,900), the row is rewritten with the
    risk (₹6 × 390) and the value re-derived, the position, the fill and the GTT all carry 390,
    and the book ends at 24.98 %."""
    store = MemoryStore(rung=0)
    _rung0_book(store)
    plan_id = store.add_plan()
    line_id = _beta_line(store, plan_id)
    out = execute(store, gw, plan_id, line_id)
    assert out.status == "SIMULATED", out
    assert sent_quantities(gw) == [390] and out.gtt["qty"] == 390
    line = store.lines[line_id]
    assert line["quantity"] == 390 and line["state"] == "FILLED"
    assert line["risk_inr"] == D("2340.00") and line["position_value"] == D("81900.00")
    assert "re-sized at confirm 833 → 390" in line["note"] and "CASH" in line["note"]
    assert store.resizes == [{"line_id": line_id, "quantity": 390, "risk_inr": D("2340.00"),
                              "position_value": D("81900.00"), "note": line["note"]}]
    pos = store.positions[out.position_id]
    assert pos["quantity_entered"] == pos["quantity_open"] == 390
    assert store.fills[-1]["quantity"] == 390
    book = sum(D(str(p["entry_avg"])) * p["quantity_open"] for p in store.positions.values())
    assert book == D("249832.80") and book <= D(250_000), "24.98 % of the sleeve, not 34 %"
    events = journal_events(gw)
    assert events[-2:] == ["dry_run", "gtt_dry_run"]


def test_confirm_EXPOSURE_FULL_when_no_sliver_fits_is_BLOCKED_and_sends_nothing(gw) -> None:
    """₹2,48,000 on the book at rung 0 leaves ₹2,000 — under the ₹10,000 minimum trade value —
    so the re-size cannot line even a sliver: `EXPOSURE_FULL`, the line `REJECTED` with the
    reason, no order, no GTT, no position, nothing in the journal."""
    store = MemoryStore(rung=0)
    store.add_position(symbol="ALPHA", instrument_id=11, entry_avg=D("124.00"),
                       quantity_entered=2000, quantity_open=2000, initial_stop=D("120.00"),
                       stop=D("120.00"))
    plan_id = store.add_plan()
    line_id = _beta_line(store, plan_id)
    out = execute(store, gw, plan_id, line_id)
    assert out.status == "BLOCKED" and out.reason.startswith("EXPOSURE_FULL: BETA"), out
    assert "leaves ₹2,000.00" in out.reason and "BELOW_MIN_TRADE_VALUE" in out.reason
    assert out.order is None and out.gtt is None and out.position_id is None
    assert store.lines[line_id]["state"] == "REJECTED"
    assert store.lines[line_id]["quantity"] == 833, "a refused line keeps its planned size"
    assert len(store.positions) == 1 and store.fills == [] and store.resizes == []
    assert journal_events(gw) == []
    assert store.sessions[NOW.date()] == {"mode": "DRY_RUN", "confirms": 1, "fills": 0,
                                          "manage_actions": 0}


def test_confirm_TIER_FULL_when_the_rung_allows_no_more_positions(gw) -> None:
    """Rung 0 allows two names. Two held (small, well under the ceiling) and a third line is
    `TIER_FULL` — the count, not the money, refuses it — and the detail names the number."""
    store = MemoryStore(rung=0)
    store.add_position(symbol="ALPHA", instrument_id=11, entry_avg=D("100.00"),
                       quantity_entered=100, quantity_open=100)
    store.add_position(symbol="BETA", instrument_id=12, entry_avg=D("200.00"),
                       quantity_entered=100, quantity_open=100, initial_stop=D("190.00"),
                       stop=D("190.00"))
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, symbol="GAMMA", instrument_id=13, quantity=50)
    out = execute(store, gw, plan_id, line_id)
    assert out.status == "BLOCKED" and out.reason.startswith("TIER_FULL: GAMMA"), out
    assert "rung 0 allows 2 positions" in out.reason
    assert len(store.positions) == 2 and journal_events(gw) == []
    assert store.lines[line_id]["state"] == "REJECTED"


def test_confirm_TIER_FULL_takes_the_persons_cap_when_it_is_below_the_rung(gw) -> None:
    """`04` §9.1: the count is `min(rung, sw_config.max_open_positions)`. Rung 3 allows ten;
    a person whose cap is 1 gets `TIER_FULL` on the second name."""
    store = MemoryStore(rung=3)
    store._config["max_open_positions"] = 1
    store.add_position(symbol="ALPHA", instrument_id=11, quantity_entered=100, quantity_open=100)
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, symbol="BETA", instrument_id=12, quantity=50)
    out = execute(store, gw, plan_id, line_id)
    assert out.status == "BLOCKED" and "TIER_FULL" in out.reason
    assert "rung 3 allows 1 positions" in out.reason


def test_confirm_SESSION_CAP_after_three_entries_today(gw) -> None:
    """`04` §5.3: three new entries a session, counted over today's CONFIRMED / SENT / FILLED
    lines whatever plan they came from. The fourth confirm is `SESSION_CAP` before its size
    is looked at — rung 3 would allow ten names and the money is there."""
    store = MemoryStore(rung=3)
    plan_id = store.add_plan()
    ids = [store.add_line(plan_id, symbol=s, instrument_id=i, quantity=10)
           for s, i in (("ALPHA", 11), ("BETA", 12), ("GAMMA", 13), ("DELTA", 14))]
    outs = [execute(store, gw, plan_id, line_id) for line_id in ids]
    assert [o.status for o in outs] == ["SIMULATED"] * 3 + ["BLOCKED"], outs
    assert outs[3].reason.startswith("SESSION_CAP: DELTA — 3 new entries per session")
    assert len(store.positions) == 3 and store.lines[ids[3]]["state"] == "REJECTED"
    assert journal_events(gw).count("dry_run") == 3


def test_SESSION_CAP_counts_a_SENT_live_line_and_not_a_REJECTED_one(gw) -> None:
    """A live order accepted and not filled (`SENT`, SW7.1) is an entry the session took; a
    refused confirm (`REJECTED`) never was one. Two SENT + one FILLED = three; a REJECTED
    fourth does not move the count, and the fifth is still the cap."""
    store = MemoryStore(rung=3)
    plan_id = store.add_plan()
    for s, i, state in (("A1", 21, "SENT"), ("A2", 22, "SENT"), ("A3", 23, "REJECTED")):
        store.add_line(plan_id, symbol=s, instrument_id=i, quantity=10, state=state)
    third = store.add_line(plan_id, symbol="A4", instrument_id=24, quantity=10)
    assert execute(store, gw, plan_id, third).status == "SIMULATED"
    fifth = store.add_line(plan_id, symbol="A5", instrument_id=25, quantity=10)
    out = execute(store, gw, plan_id, fifth)
    assert out.status == "BLOCKED" and "SESSION_CAP" in out.reason
    ctx = store.session_context(NOW.date())
    assert ctx.entries_today == 3
    assert ctx.account.open_symbols == frozenset({"A1", "A2", "A4"}), "SENT lines are held"


def test_a_SENT_line_counts_as_exposure_at_its_trigger(gw) -> None:
    """The shares of a live order not yet filled may arrive any moment: they are on the book
    at the trigger for the ceiling, so the next confirm cannot spend the same rupees twice."""
    store = MemoryStore(rung=0)
    plan_id = store.add_plan()
    store.add_line(plan_id, symbol="ALPHA", instrument_id=11, quantity=1666,
                   trigger=D("100.80"), stop=D("97.80"), state="SENT")
    line_id = _beta_line(store, plan_id)
    out = execute(store, gw, plan_id, line_id)
    assert out.status == "SIMULATED" and sent_quantities(gw) == [390], out
    assert store.lines[line_id]["quantity"] == 390


def test_confirm_never_sends_more_than_the_line_said(gw) -> None:
    """The page said 100; the rules would allow 1,666. 100 goes, the row is not touched."""
    store = MemoryStore(rung=3)
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, quantity=100)
    out = execute(store, gw, plan_id, line_id)
    assert out.status == "SIMULATED" and sent_quantities(gw) == [100]
    assert store.resizes == [] and store.lines[line_id]["quantity"] == 100
    assert store.lines[line_id]["note"] == "", "an unchanged line keeps its note"


def test_the_line_being_confirmed_is_not_counted_against_itself(gw) -> None:
    """The context is read before the line is marked CONFIRMED: a lone line at rung 0 is
    neither `ALREADY_HELD` by its own symbol nor an entry against its own cap."""
    store = MemoryStore(rung=0)
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, quantity=1666, trigger=D("100.80"), stop=D("97.80"))
    out = execute(store, gw, plan_id, line_id)
    assert out.status == "SIMULATED" and sent_quantities(gw) == [1666], out
    assert store.context_reads == 1
    assert store.line_history[-2:] == [(line_id, "CONFIRMED"), (line_id, "FILLED")]


def test_confirm_re_sizes_with_the_persons_risk_knob(gw) -> None:
    """MD6: a SIGNAL line sized by the pack's 0.5 % is re-sized at confirm with the person's
    `sw_config.risk_per_trade_pct`. At 0.25 % over a ₹3 stop the budget is ₹2,500 → 833."""
    store = MemoryStore(rung=3)
    store._config["risk_per_trade_pct"] = D("0.250")
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, quantity=1666, trigger=D("100.80"), stop=D("97.80"))
    out = execute(store, gw, plan_id, line_id)
    assert out.status == "SIMULATED" and sent_quantities(gw) == [833]
    assert "re-sized at confirm 1666 → 833 (size by RISK)" in store.lines[line_id]["note"]


@pytest.mark.parametrize(
    ("market", "code"),
    [({"gate": "RED", "new_entries_allowed": False}, "GATE_RED"),
     ({"drawdown_locked": True}, "DRAWDOWN_LOCKOUT")],
)
def test_confirm_refuses_under_a_red_gate_or_a_drawdown_lock(gw, market, code) -> None:
    """`04` §8.3 / §8.5 apply at the click as they apply to the plan: the market row is read
    again, and a line built before the evening turned the gate red does not go."""
    store = MemoryStore(rung=1)
    store.market.update(market)
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id)
    out = execute(store, gw, plan_id, line_id)
    assert out.status == "BLOCKED" and out.reason.startswith(f"{code}: ALPHA")
    assert journal_events(gw) == [] and store.positions == {}


def test_confirm_refuses_a_name_with_no_ADR_on_record(gw) -> None:
    """SW9.5.2 at the desk: the widest stop is one ADR, and a name with no detection row has
    no ADR to check a stop against. `SIZE_REFUSED`, and the reason says so."""
    store = MemoryStore()
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id)
    del store.detected["ALPHA"]
    out = execute(store, gw, plan_id, line_id)
    assert out.status == "BLOCKED" and out.reason.startswith("SIZE_REFUSED: ALPHA")
    assert "ADR is unknown" in out.reason and journal_events(gw) == []


def test_confirm_refuses_a_stop_wider_than_one_ADR(gw) -> None:
    """`04` §6.1: a 6 % stop on a 5 % ADR name is `STOP_TOO_WIDE` — at confirm too."""
    store = MemoryStore()
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, trigger=D("100.00"), stop=D("94.00"))
    out = execute(store, gw, plan_id, line_id)
    assert out.status == "BLOCKED" and out.reason == "SIZE_REFUSED: ALPHA — STOP_TOO_WIDE"


def test_confirm_refuses_a_setup_this_book_does_not_trade(gw) -> None:
    store = MemoryStore()
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, setup="PARABOLIC_SHORT")
    out = execute(store, gw, plan_id, line_id)
    assert out.status == "BLOCKED" and out.reason.startswith("NOT_TRADEABLE_SETUP")
    line_id = store.add_line(plan_id, setup=None)
    out = execute(store, gw, plan_id, line_id)
    assert out.status == "BLOCKED" and out.reason.startswith("NOT_TRADEABLE_SETUP")


def test_a_partial_then_full_sequence_of_confirms_holds_the_ceiling_and_the_count(gw) -> None:
    """The drill's morning, then one more: at rung 0 ALPHA goes whole (16.79 %), BETA is
    re-sized to the ceiling (24.98 %), and GAMMA — the third name at a rung that allows two —
    is `TIER_FULL`. Every confirm took the session lock; the book never passed 25 %."""
    store = MemoryStore(rung=0)
    plan_id = store.add_plan()
    alpha = store.add_line(plan_id, quantity=1666, trigger=D("100.80"), stop=D("97.80"))
    beta = _beta_line(store, plan_id)
    store.detected["GAMMA"] = (D("5.00"), D(100_000_000), D("65.00"))
    gamma = store.add_line(plan_id, symbol="GAMMA", instrument_id=13, quantity=10)
    outs = [execute(store, gw, plan_id, line_id) for line_id in (alpha, beta, gamma)]
    assert [o.status for o in outs] == ["SIMULATED", "SIMULATED", "BLOCKED"], outs
    assert sent_quantities(gw) == [1666, 390]
    assert outs[2].reason.startswith("TIER_FULL: GAMMA")
    book = sum(D(str(p["entry_avg"])) * p["quantity_open"] for p in store.positions.values())
    assert book == D("249832.80") and book <= D(250_000)
    assert store.locks == [NOW.date()] * 3
    assert store.sessions[NOW.date()]["confirms"] == 3
    assert store.sessions[NOW.date()]["fills"] == 2


def test_the_lock_is_taken_for_every_kind_and_spans_the_gateway_call(gw) -> None:
    """A SELL and a RAISE take the session lock too — the book they change is the book the
    next BUY is sized against — and the lock is held while the gateway answers: the gateway
    call happens between lock entry and lock exit, never outside."""
    order = []

    class Recording(MemoryStore):
        @contextlib.contextmanager
        def lock_session_for_update(self, day):
            order.append("lock")
            with super().lock_session_for_update(day):
                yield
            order.append("unlock")

    class Watched:
        def __init__(self, real):
            self.real = real

        def __getattr__(self, name):
            attr = getattr(self.real, name)
            if name in ("place", "place_gtt_stop", "delete_gtt"):
                async def call(**kw):
                    order.append(name)
                    return await attr(**kw)
                return call
            return attr

    store = Recording()
    pid = store.add_position(quantity_entered=300, quantity_open=300, gtt_id="4242")
    plan_id = store.add_plan()
    sell = store.add_line(plan_id, kind="SELL_AT_OPEN", quantity=100, trigger=None, stop=None)
    raise_ = store.add_line(plan_id, kind="RAISE_GTT_STOP", quantity=0, trigger=None,
                            stop=D("98.00"))
    execute(store, Watched(gw), plan_id, sell, last_price=D("104.00"))
    execute(store, Watched(gw), plan_id, raise_, last_price=D("104.00"))
    assert order == ["lock", "place", "delete_gtt", "place_gtt_stop", "unlock",
                     "lock", "place_gtt_stop", "unlock"], order
    assert store.positions[pid]["quantity_open"] == 200 and store.positions[pid]["stop"] == D("98.00")


def test_a_gateway_exception_rolls_the_lock_back_and_records_the_refusal_once(gw) -> None:
    """Under the lock an untouchable instrument raises out of the gateway: the transaction
    rolls back (the CONFIRMED mark and the counter with it), then the line is marked
    REJECTED and the click counted, once — the same end state SW7 promised, reached through
    the rollback rather than around it."""
    from baskfy_execution.guards import UntouchableInstrumentError

    store = MemoryStore()
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id, symbol="SGBAUG28")
    with pytest.raises(UntouchableInstrumentError):
        execute(store, gw, plan_id, line_id)
    assert store.lines[line_id]["state"] == "REJECTED"
    assert store.sessions[NOW.date()]["confirms"] == 1
    assert store.positions == {} and store.fills == []


def test_a_line_confirmed_by_another_request_under_the_lock_is_409(gw) -> None:
    """Two requests pass `_validate` on the same PROPOSED line; the second, once it holds the
    lock, reads the line again and finds it FILLED — 409, nothing sent, nothing written."""
    store = MemoryStore()
    plan_id = store.add_plan()
    line_id = store.add_line(plan_id)

    class Racing(MemoryStore):
        pass

    real_lock = store.lock_session_for_update

    @contextlib.contextmanager
    def lock_then_lose(day):
        with real_lock(day):
            store.lines[line_id]["state"] = "FILLED"   # the other tab won the lock first
            yield

    store.lock_session_for_update = lock_then_lose
    with pytest.raises(HTTPException) as exc:
        execute(store, gw, plan_id, line_id)
    assert exc.value.status_code == 409 and "FILLED" in exc.value.detail
    assert journal_events(gw) == [] and store.positions == {}
    assert store.sessions == {}, "no confirm was counted for a line that was already gone"


def test_sizing_config_carries_the_three_knobs_and_nothing_else() -> None:
    config = X.sizing_config({"risk_per_trade_pct": D("0.250"), "max_position_pct": D("15.00"),
                              "max_open_positions": 4, "sleeve_capital_inr": D("1")})
    assert (config.sizing.risk_per_trade_pct, config.sizing.max_position_pct,
            config.sizing.max_open_positions) == (0.25, 15.0, 4)
    assert config.sizing.max_new_entries_per_session == 3, "the session cap is not a setting"
    assert config.stops == X.DEFAULT_SWING_CONFIG.stops and config.market == X.DEFAULT_SWING_CONFIG.market
    assert X.sizing_config({}).sizing == X.DEFAULT_SWING_CONFIG.sizing
