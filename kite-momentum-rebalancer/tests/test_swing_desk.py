"""The swing desk page, its store and its route (SW7, the page half — `docs/swing/05` §3).

What is asserted, and where it comes from:

* `05` §3: three panels and a status bar; a TRIGGERED signal with a Confirm and its plan
  countdown; a LOCKED_UPPER_CIRCUIT signal without one; every line prefixed SWING; exits
  first, then waiting buys, then skips with reasons; Re-arm GTT for a naked position and only
  a naked one; the last five manage actions; "There is no 'confirm all'".
* Contract C1: `POST /swing/execute` goes through `app.swing_execute.execute_line` and returns
  its outcome as JSON; 400 without confirm, 410 past expiry — through the route.
* Contract C1 `SwingStore`: every method round-trips against sqlite with Decimal in and out.
* The second law, restated for a page: the module never names a placing verb or the broker.

THE DATABASE
------------
The `sw_` tables are built here from a DDL that mirrors `alembic/versions/0028_swing.py`
column for column (types in sqlite's spelling), on a per-test sqlite file, and the store runs
the same SQL it runs on the desk's Postgres with the schema prefix set to "". A desk test
suite cannot depend on a Postgres being up (`DECISIONS-SW.md` SW7.3 says why this, and not a
fake connection, is the honest fixture).

THE EXECUTE MODULE
------------------
`app.swing_execute` is leaf 1.1.1's, built concurrently. When it is importable the tests use
it and monkeypatch its `execute_line`/`rearm_gtt` with fakes that honour C1's HTTP semantics;
when it is not, a stub module is placed in `sys.modules` under the same name so the route's
`from . import swing_execute` resolves. Either way the route is exercised through the
attribute the sibling owns, so these tests pass before and after it lands.
"""
from __future__ import annotations

import ast
import contextlib
import dataclasses
import datetime as dt
import inspect
import re
import sqlite3
import sys
import types
import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import config as C
from app import main as M
from app import swing_desk
from app.swing_desk import IST, PgSwingStore, build_view, monitor_state

USER = 1
TODAY = dt.date(2026, 9, 2)  # a Wednesday
NOW = dt.datetime(2026, 9, 2, 9, 40, tzinfo=IST)

# ---------------------------------------------------------------------------------------
# The DDL: 0028_swing.py, column for column, in sqlite's spelling. NUMERIC columns keep
# NUMERIC affinity so the store's Decimal handling is exercised against what sqlite
# actually returns (a float), which is also what the desk's Postgres adapter returns.
# ---------------------------------------------------------------------------------------
DDL = [
    """CREATE TABLE instrument(
        id INTEGER PRIMARY KEY, symbol TEXT NOT NULL UNIQUE, name TEXT, kite_token INTEGER)""",
    """CREATE TABLE sw_config(
        user_id INTEGER PRIMARY KEY,
        sleeve_capital_inr NUMERIC NOT NULL DEFAULT 0,
        risk_per_trade_pct NUMERIC NOT NULL DEFAULT 0.500,
        max_position_pct NUMERIC NOT NULL DEFAULT 20.00,
        max_open_positions INTEGER NOT NULL DEFAULT 8,
        or_window_minutes INTEGER NOT NULL DEFAULT 5,
        stop_mode TEXT NOT NULL DEFAULT 'LOW_OF_DAY',
        adr_min_pct NUMERIC NOT NULL DEFAULT 3.50,
        turnover_min_inr NUMERIC NOT NULL DEFAULT 50000000,
        price_min NUMERIC NOT NULL DEFAULT 20.00,
        exposure_level INTEGER NOT NULL DEFAULT 0,
        first_live_sessions_left INTEGER NOT NULL DEFAULT 5,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_by TEXT,
        CHECK (first_live_sessions_left >= 0),
        CHECK (exposure_level >= 0 AND exposure_level <= 3))""",
    """CREATE TABLE sw_plan(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id TEXT NOT NULL UNIQUE,
        user_id INTEGER NOT NULL,
        as_of TEXT NOT NULL,
        source TEXT NOT NULL,
        built_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        plan_hash TEXT NOT NULL,
        gate TEXT NOT NULL,
        exposure_level INTEGER NOT NULL,
        total_risk_inr NUMERIC NOT NULL DEFAULT 0,
        total_new_exposure_inr NUMERIC NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CHECK (gate IN ('GREEN', 'AMBER', 'RED')),
        CHECK (source IN ('EOD_PREVIEW', 'MORNING', 'SIGNAL')))""",
    """CREATE TABLE sw_plan_line(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id INTEGER NOT NULL REFERENCES sw_plan(id),
        user_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        instrument_id INTEGER NOT NULL REFERENCES instrument(id),
        setup TEXT,
        quantity INTEGER NOT NULL DEFAULT 0,
        trigger NUMERIC,
        stop NUMERIC,
        risk_inr NUMERIC NOT NULL DEFAULT 0,
        position_value NUMERIC NOT NULL DEFAULT 0,
        trail TEXT,
        note TEXT,
        state TEXT NOT NULL DEFAULT 'PROPOSED',
        client_id TEXT NOT NULL UNIQUE,
        journal_ref TEXT,
        position_id INTEGER,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CHECK (kind IN ('BUY_ON_TRIGGER', 'SELL_AT_OPEN', 'RAISE_GTT_STOP')),
        CHECK (state IN ('PROPOSED','CONFIRMED','SENT','FILLED','REJECTED','EXPIRED','SKIPPED')))""",
    """CREATE TABLE sw_plan_skip(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id INTEGER NOT NULL REFERENCES sw_plan(id),
        user_id INTEGER NOT NULL,
        instrument_id INTEGER,
        symbol TEXT NOT NULL,
        reason TEXT NOT NULL,
        detail TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE sw_watch(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        instrument_id INTEGER NOT NULL REFERENCES instrument(id),
        setup TEXT NOT NULL, source TEXT NOT NULL,
        added_on TEXT NOT NULL, expires_on TEXT,
        trigger NUMERIC, stop_ref NUMERIC, setup_daily_date TEXT,
        note TEXT, catalyst TEXT,
        state TEXT NOT NULL DEFAULT 'WATCHING',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE sw_signal(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        watch_id INTEGER REFERENCES sw_watch(id),
        instrument_id INTEGER NOT NULL REFERENCES instrument(id),
        setup TEXT NOT NULL,
        session_date TEXT NOT NULL,
        raised_at TEXT NOT NULL,
        state TEXT NOT NULL,
        or_window_minutes INTEGER,
        range_high NUMERIC, range_low NUMERIC, low_of_day NUMERIC, last_price NUMERIC,
        entry NUMERIC, stop NUMERIC,
        plan_line_id INTEGER REFERENCES sw_plan_line(id),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE sw_position(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        broker_account_id INTEGER,
        instrument_id INTEGER NOT NULL REFERENCES instrument(id),
        setup TEXT NOT NULL,
        entry_date TEXT NOT NULL,
        entry_avg NUMERIC NOT NULL,
        quantity_entered INTEGER NOT NULL,
        initial_stop NUMERIC NOT NULL,
        stop NUMERIC NOT NULL,
        gtt_id TEXT,
        gtt_trigger NUMERIC,
        gtt_armed_at TEXT,
        trail TEXT NOT NULL,
        partial_done BOOLEAN NOT NULL DEFAULT false,
        partial_date TEXT,
        quantity_open INTEGER NOT NULL,
        state TEXT NOT NULL DEFAULT 'OPEN',
        closed_on TEXT,
        exit_avg NUMERIC,
        close_reason TEXT,
        r_multiple NUMERIC,
        pnl_inr NUMERIC,
        simulated BOOLEAN NOT NULL DEFAULT true,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CHECK (setup IN ('FLAG', 'EP', 'PARABOLIC_SHORT')),
        CHECK (state IN ('OPEN', 'PARTIAL', 'CLOSED')),
        CHECK (trail IN ('MA10', 'MA20')),
        CHECK (initial_stop < entry_avg),
        CHECK (quantity_open <= quantity_entered),
        CHECK (quantity_open >= 0),
        CHECK (stop >= initial_stop))""",
    """CREATE TABLE sw_fill(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        position_id INTEGER NOT NULL REFERENCES sw_position(id),
        side TEXT NOT NULL,
        quantity INTEGER NOT NULL,
        price NUMERIC NOT NULL,
        filled_at TEXT NOT NULL,
        journal_ref TEXT,
        simulated BOOLEAN NOT NULL DEFAULT true,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CHECK (side IN ('BUY', 'SELL')),
        CHECK (quantity > 0))""",
    """CREATE TABLE sw_session(
        user_id INTEGER NOT NULL,
        session_date TEXT NOT NULL,
        mode TEXT NOT NULL,
        monitor_ran BOOLEAN NOT NULL DEFAULT false,
        plan_ids TEXT,
        signals INTEGER NOT NULL DEFAULT 0,
        confirms INTEGER NOT NULL DEFAULT 0,
        fills INTEGER NOT NULL DEFAULT 0,
        manage_actions INTEGER NOT NULL DEFAULT 0,
        notes TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, session_date),
        CHECK (mode IN ('DRY_RUN', 'LIVE')))""",
]


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---------------------------------------------------------------------------------------
# A small scenario builder: instruments, a config row, plans with lines and skips, signals,
# positions. Every price is written as the store would find it on a real database.
# ---------------------------------------------------------------------------------------
class Scenario:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn = _connect(path)
        for ddl in DDL:
            self.conn.execute(ddl)
        self.conn.execute(
            "INSERT INTO instrument(id, symbol, name, kite_token) VALUES "
            "(1,'ALPHAFLAG','Alpha Flag Ltd',111),(2,'BETAEP','Beta EP Ltd',222),"
            "(3,'GAMMALOCK','Gamma Lock Ltd',333),(4,'DELTAHELD','Delta Held Ltd',444),"
            "(5,'EPSILON','Epsilon Ltd',555)"
        )

    def store(self) -> PgSwingStore:
        return PgSwingStore(_connect(self.path), user_id=USER, schema="", broker_account_id=1)

    def config(self, capital: str = "1000000", risk: str = "0.500", first_live: int = 5) -> None:
        self.conn.execute(
            "INSERT INTO sw_config(user_id, sleeve_capital_inr, risk_per_trade_pct, "
            "first_live_sessions_left) VALUES (?, ?, ?, ?)",
            (USER, capital, risk, first_live),
        )

    def plan(self, *, source: str, built_at: dt.datetime, as_of: dt.date = TODAY,
             gate: str = "GREEN", ttl_minutes: int = 30) -> tuple[int, str]:
        plan_id = str(uuid.uuid4())
        pk = self.conn.execute(
            "INSERT INTO sw_plan(plan_id, user_id, as_of, source, built_at, expires_at, "
            "plan_hash, gate, exposure_level, total_risk_inr, total_new_exposure_inr) "
            "VALUES (?, ?, ?, ?, ?, ?, 'h', ?, 2, '5000.00', '150000.00') RETURNING id",
            (plan_id, USER, as_of.isoformat(), source, built_at.isoformat(),
             (built_at + dt.timedelta(minutes=ttl_minutes)).isoformat(), gate),
        ).fetchone()["id"]
        return int(pk), plan_id

    def line(self, plan_pk: int, plan_id: str, *, kind: str, instrument_id: int, symbol: str,
             quantity: int = 100, trigger: str | None = "100.80", stop: str | None = "97.80",
             state: str = "PROPOSED", note: str | None = None, setup: str | None = "FLAG") -> int:
        return int(self.conn.execute(
            "INSERT INTO sw_plan_line(plan_id, user_id, kind, instrument_id, setup, quantity, "
            "trigger, stop, risk_inr, position_value, trail, note, state, client_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, '300.00', '10080.00', 'MA10', ?, ?, ?) RETURNING id",
            (plan_pk, USER, kind, instrument_id, setup, quantity, trigger, stop, note, state,
             f"{plan_id}:{symbol}:{kind}"),
        ).fetchone()["id"])

    def skip(self, plan_pk: int, *, instrument_id: int, symbol: str, reason: str,
             detail: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO sw_plan_skip(plan_id, user_id, instrument_id, symbol, reason, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)", (plan_pk, USER, instrument_id, symbol, reason, detail))

    def signal(self, *, instrument_id: int, setup: str, state: str, raised_at: dt.datetime,
               range_high: str = "100.50", last_price: str = "100.80",
               entry: str | None = "100.80", stop: str | None = "97.80",
               plan_line_id: int | None = None) -> int:
        return int(self.conn.execute(
            "INSERT INTO sw_signal(user_id, instrument_id, setup, session_date, raised_at, state, "
            "or_window_minutes, range_high, range_low, low_of_day, last_price, entry, stop, "
            "plan_line_id) VALUES (?, ?, ?, ?, ?, ?, 5, ?, '98.00', '97.80', ?, ?, ?, ?) "
            "RETURNING id",
            (USER, instrument_id, setup, TODAY.isoformat(), raised_at.isoformat(), state,
             range_high, last_price, entry, stop, plan_line_id),
        ).fetchone()["id"])

    def position(self, *, instrument_id: int, entry_avg: str = "100.80", quantity: int = 300,
                 quantity_open: int | None = None, stop: str = "97.80", gtt_id: str | None = "G1",
                 state: str = "OPEN", simulated: bool = True,
                 initial_stop: str | None = None) -> int:
        return int(self.conn.execute(
            "INSERT INTO sw_position(user_id, broker_account_id, instrument_id, setup, entry_date, "
            "entry_avg, quantity_entered, initial_stop, stop, gtt_id, gtt_trigger, gtt_armed_at, "
            "trail, quantity_open, state, simulated) VALUES (?, 1, ?, 'FLAG', ?, ?, ?, ?, ?, "
            "?, ?, ?, 'MA10', ?, ?, ?) RETURNING id",
            (USER, instrument_id, (TODAY - dt.timedelta(days=3)).isoformat(), entry_avg, quantity,
             initial_stop or stop, stop, gtt_id, stop if gtt_id else None,
             (NOW - dt.timedelta(days=3)).isoformat() if gtt_id else None,
             quantity if quantity_open is None else quantity_open, state, simulated),
        ).fetchone()["id"])

    def session(self, **counters: int | bool) -> None:
        cols = {"mode": "DRY_RUN", "monitor_ran": False, "signals": 0, "confirms": 0, "fills": 0,
                "manage_actions": 0, **counters}
        self.conn.execute(
            "INSERT INTO sw_session(user_id, session_date, mode, monitor_ran, signals, confirms, "
            "fills, manage_actions) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (USER, TODAY.isoformat(), cols["mode"], cols["monitor_ran"], cols["signals"],
             cols["confirms"], cols["fills"], cols["manage_actions"]),
        )

    # -- the morning as the page would find it at 09:40 -------------------------------------
    def morning(self) -> dict:
        """A MORNING plan with a SELL, a RAISE, two waiting buys (one already held) and a skip;
        a SIGNAL plan for ALPHAFLAG's 09:31 trigger; a locked GAMMALOCK; a skipped EPSILON
        trigger; one armed and one naked position; last night's EOD preview."""
        self.config()
        ids: dict = {}
        ids["held"] = self.position(instrument_id=4, gtt_id="G-44")
        ids["naked"] = self.position(instrument_id=2, gtt_id=None, entry_avg="210.50",
                                     stop="204.50")
        preview_pk, preview_id = self.plan(
            source="EOD_PREVIEW", built_at=dt.datetime(2026, 9, 1, 21, 5, tzinfo=IST),
            as_of=dt.date(2026, 9, 1))
        self.line(preview_pk, preview_id, kind="SELL_AT_OPEN", instrument_id=4,
                  symbol="DELTAHELD", quantity=100, trigger=None, stop=None,
                  note="partial into strength, day 3")
        # Built at 09:12 so that at 09:40 the plan has 2:00 left — not sitting on the
        # 30-minute boundary, where `now == expires_at` is still good (the sibling's rule)
        # but the countdown already reads 0:00.
        morning_pk, morning_id = self.plan(
            source="MORNING", built_at=dt.datetime(2026, 9, 2, 9, 12, tzinfo=IST))
        ids["morning"] = (morning_pk, morning_id)
        ids["sell"] = self.line(morning_pk, morning_id, kind="SELL_AT_OPEN", instrument_id=4,
                                symbol="DELTAHELD", quantity=100, trigger=None, stop=None,
                                note="partial into strength, day 3")
        ids["raise"] = self.line(morning_pk, morning_id, kind="RAISE_GTT_STOP", instrument_id=4,
                                 symbol="DELTAHELD", quantity=0, trigger=None, stop="100.80",
                                 note="breakeven after partial")
        ids["waiting"] = self.line(morning_pk, morning_id, kind="BUY_ON_TRIGGER", instrument_id=1,
                                   symbol="ALPHAFLAG", quantity=1666)
        ids["waiting_held"] = self.line(morning_pk, morning_id, kind="BUY_ON_TRIGGER",
                                        instrument_id=2, symbol="BETAEP", quantity=200,
                                        trigger="212.00", stop="204.50", setup="EP")
        self.skip(morning_pk, instrument_id=3, symbol="GAMMALOCK", reason="LOCKED_UPPER_CIRCUIT",
                  detail="upper circuit at 52.50")
        signal_pk, signal_id = self.plan(
            source="SIGNAL", built_at=dt.datetime(2026, 9, 2, 9, 31, tzinfo=IST))
        ids["signal_plan"] = (signal_pk, signal_id)
        ids["trigger_line"] = self.line(signal_pk, signal_id, kind="BUY_ON_TRIGGER",
                                        instrument_id=1, symbol="ALPHAFLAG", quantity=1666)
        ids["trigger"] = self.signal(instrument_id=1, setup="FLAG", state="TRIGGERED",
                                     raised_at=dt.datetime(2026, 9, 2, 9, 31, tzinfo=IST),
                                     plan_line_id=ids["trigger_line"])
        ids["locked"] = self.signal(instrument_id=3, setup="FLAG", state="LOCKED_UPPER_CIRCUIT",
                                    raised_at=dt.datetime(2026, 9, 2, 9, 20, tzinfo=IST),
                                    range_high="52.50", last_price="52.50", entry=None,
                                    stop=None)
        skipped_pk, _ = self.plan(source="SIGNAL",
                                  built_at=dt.datetime(2026, 9, 2, 9, 35, tzinfo=IST))
        self.skip(skipped_pk, instrument_id=5, symbol="EPSILON", reason="TIER_FULL",
                  detail="2 of 2 positions open")
        ids["skipped"] = self.signal(instrument_id=5, setup="FLAG", state="TRIGGERED",
                                     raised_at=dt.datetime(2026, 9, 2, 9, 35, tzinfo=IST),
                                     range_high="300.00", last_price="300.50", entry="300.50",
                                     stop="295.00")
        self.session(monitor_ran=False, signals=3, confirms=1, fills=1, manage_actions=0)
        return ids


@pytest.fixture()
def scenario(tmp_path):
    return Scenario(str(tmp_path / "swing.db"))


@pytest.fixture()
def store(scenario):
    return scenario.store()


# ---------------------------------------------------------------------------------------
# The execute module: real if it exists, a stub under the same name if it does not.
# ---------------------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class FakeOutcome:
    status: str
    reason: str
    order: dict | None
    gtt: dict | None
    position_id: int | None
    simulated: bool


@pytest.fixture()
def exec_module(monkeypatch):
    try:
        import app.swing_execute as real  # noqa: PLC0415
        return real
    except ImportError:
        stub = types.ModuleType("app.swing_execute")
        monkeypatch.setitem(sys.modules, "app.swing_execute", stub)
        return stub


@pytest.fixture()
def fake_execute(exec_module, monkeypatch):
    """A stand-in for `execute_line` with C1's HTTP semantics, recording what it was given.

    It reads the plan and the line through the store it is handed — so the route is proven to
    pass a working store — and raises 400/404/410/409 exactly where the contract says."""
    calls: list[dict] = []

    async def execute_line(store, gw, *, plan_id, line_id, confirm, now, last_price=None):
        calls.append({"store": store, "gw": gw, "plan_id": plan_id, "line_id": line_id,
                      "confirm": confirm, "now": now, "last_price": last_price})
        if confirm != "true":
            raise HTTPException(400, "Execution requires explicit confirmation.")
        plan = store.plan(plan_id)
        line = store.line(line_id)
        if plan is None or line is None or line["plan_id"] != plan["plan_id"]:
            raise HTTPException(404, "Unknown plan or line.")
        if now > plan["expires_at"]:
            raise HTTPException(410, "Plan older than 30 minutes.")
        if line["state"] != "PROPOSED":
            raise HTTPException(409, f"line is {line['state']}")
        store.set_line(line_id, state="FILLED", journal_ref="sim-1")
        store.bump_session(now.date(), mode="DRY_RUN", confirms=1, fills=1)
        return FakeOutcome(status="SIMULATED", reason="", order={"status": "DRY_RUN"},
                           gtt={"status": "DRY_RUN_GTT", "trigger": Decimal("97.80")},
                           position_id=7, simulated=True)

    async def rearm_gtt(store, gw, *, position_id, confirm, now, last_price=None):
        calls.append({"store": store, "gw": gw, "position_id": position_id, "confirm": confirm,
                      "last_price": last_price})
        if confirm != "true":
            raise HTTPException(400, "Re-arming requires explicit confirmation.")
        pos = store.position(position_id)
        if pos is None:
            raise HTTPException(404, "not a swing position")
        if pos["gtt_id"] is not None:
            return FakeOutcome("BLOCKED", "position already carries a GTT", None, None,
                               position_id, True)
        return FakeOutcome("SIMULATED", "", None, {"status": "DRY_RUN_GTT"}, position_id, True)

    monkeypatch.setattr(exec_module, "execute_line", execute_line, raising=False)
    monkeypatch.setattr(exec_module, "rearm_gtt", rearm_gtt, raising=False)
    return calls


@pytest.fixture()
def client(scenario, monkeypatch, tmp_path):
    """The mounted app with the store pointed at the scenario's sqlite file, the clock at
    09:40 on the scenario's day, a fake gateway, and no Kite token on disk."""
    monkeypatch.setattr(M, "_kite", None)
    monkeypatch.setattr(swing_desk, "open_store",
                        lambda: contextlib.nullcontext(scenario.store()))
    monkeypatch.setattr(swing_desk, "_now", lambda: NOW)
    monkeypatch.setattr(swing_desk, "swing_gateway", lambda: "fake-gateway")
    monkeypatch.setattr(swing_desk, "last_price", lambda symbol: LAST_PRICES.get(symbol))
    monkeypatch.setattr(C, "TOKEN_FILE", str(tmp_path / "no-such-token.json"))
    return TestClient(M.app)


#: What the broker would quote, per symbol, when the route asks for a last price.
LAST_PRICES = {"DELTAHELD": Decimal("104.00"), "BETAEP": Decimal("215.00")}


def _forms(html: str, action: str) -> list[str]:
    return re.findall(r'<form[^>]*action="' + re.escape(action) + r'"[^>]*>.*?</form>', html, re.S)


# =======================================================================================
# G1 — the page renders with the three panels and the status bar
# =======================================================================================
class TestRender:
    def test_render_is_200_with_the_three_panels(self, client, scenario):
        scenario.morning()
        r = client.get("/swing")
        assert r.status_code == 200
        html = r.text
        assert '<section id="triggers">' in html
        assert '<section id="plan">' in html
        assert '<section id="book">' in html
        assert html.index('id="triggers"') < html.index('id="plan"') < html.index('id="book"')

    def test_render_of_an_empty_book_still_says_why(self, client, scenario):
        """A desk with nothing to confirm is a desk that needs to say so."""
        scenario.config()
        html = client.get("/swing").text
        assert "No signals today" in html
        assert "No morning plan for 2026-09-02" in html
        assert "No open swing positions" in html
        assert "Nothing managed yet" in html

    def test_render_when_the_store_cannot_be_read_is_a_banner_not_a_500(self, client, monkeypatch):
        @contextlib.contextmanager
        def broken():
            raise RuntimeError("no such table: sw_plan")
            yield  # pragma: no cover

        monkeypatch.setattr(swing_desk, "open_store", broken)
        r = client.get("/swing")
        assert r.status_code == 200
        assert "unavailable" in r.text and "no such table" in r.text
        assert "DRY_RUN" in r.text  # the status bar still renders

    def test_panels_order_exits_then_waiting_buys_then_skips(self, client, scenario):
        scenario.morning()
        html = client.get("/swing").text
        plan = html[html.index('<section id="plan">'):html.index('<section id="book">')]
        sell = plan.index("SWING SELL DELTAHELD")
        raise_ = plan.index("SWING RAISE GTT DELTAHELD")
        waiting = plan.index("SWING BUY ALPHAFLAG")
        skip = plan.index("LOCKED_UPPER_CIRCUIT")
        assert sell < raise_ < waiting < skip
        assert "upper circuit at 52.50" in plan  # the skip carries its detail

    def test_panels_book_shows_gtt_ids_naked_and_the_manage_actions(self, client, scenario):
        scenario.morning()
        html = client.get("/swing").text
        book = html[html.index('<section id="book">'):]
        assert "#G-44" in book
        assert "naked" in book
        assert "1 without a stop" in book
        assert "Re-arm GTT" in book
        assert "manage action" in book
        # the manage list is exit-kind lines, newest first; the morning's two are in it
        assert "SWING SELL DELTAHELD x100 at open" in book
        assert "SWING RAISE GTT DELTAHELD to 100.80" in book

    def test_panels_data_route_is_the_same_view_as_json(self, client, scenario):
        scenario.morning()
        j = client.get("/swing/data").json()
        assert j["available"] is True
        assert {"triggers", "plans", "book", "status", "fingerprint", "poll_ms"} <= set(j)
        assert len(j["triggers"]) == 3
        assert j["plans"]["morning"]["exits"][0]["kind"] == "SELL_AT_OPEN"
        # Decimals cross as strings — money is never a float on the wire
        assert j["plans"]["morning"]["buys"][0]["trigger"] == "100.80"
        assert j["poll_ms"] == 5000  # 09:40 is inside the window


class TestStatusBar:
    def test_status_bar_carries_the_flags_the_monitor_the_token_and_the_counters(self, client, scenario):
        scenario.morning()
        html = client.get("/swing").text
        bar = html[html.index('id="swStatus"'):html.index('<section id="triggers">')]
        assert "DRY_RUN</b>=<code>true</code>" in bar
        assert "BASKFY_SWING_EXECUTION_ENABLED</b>=<code>false</code>" in bar
        assert "not enabled" in bar  # the monitor flag is off in every test environment
        assert "no token" in bar
        assert "signals 3" in bar and "confirms 1" in bar and "fills 1" in bar
        assert "manage actions 0" in bar
        assert "SIMULATED" in bar

    def test_status_bar_reads_the_session_row_when_there_is_none(self, client, scenario):
        scenario.config()
        bar = client.get("/swing").text
        assert "no <code>sw_session</code> row yet" in bar
        assert "signals 0" in bar

    def test_status_bar_shows_the_token_age_when_a_token_exists(self, scenario, tmp_path, monkeypatch):
        from baskfy_providers.tokens import AccessTokenStore
        from cryptography.fernet import Fernet
        key = Fernet.generate_key().decode()
        path = tmp_path / "tok.json"
        AccessTokenStore(path, key).save("secret", issued_at=NOW - dt.timedelta(hours=2, minutes=5))
        monkeypatch.setenv("KITE_TOKEN_ENCRYPTION_KEY", key)
        info = swing_desk.token_age(NOW, path)
        assert info["present"] is True and info["expired"] is False
        assert info["age_minutes"] == 125 and info["label"] == "2h 05m old"
        stale = swing_desk.token_age(NOW + dt.timedelta(days=1), path)
        assert stale["expired"] is True and stale["label"].startswith("expired")

    def test_status_bar_token_lookup_never_creates_a_key_file(self, tmp_path):
        """Building the token store with no key writes one beside the token; a status bar
        that is merely looked at must not."""
        path = tmp_path / "absent.json"
        swing_desk.token_age(NOW, path)
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize(
        "enabled, ran, when, state",
        [
            (False, False, dt.time(9, 40), "not enabled"),
            (True, False, dt.time(8, 50), "idle"),
            (True, False, dt.time(9, 40), "running"),
            (True, True, dt.time(11, 0), "stopped"),
            (True, False, dt.time(11, 0), "did not run"),
        ],
    )
    def test_status_bar_monitor_state_is_derived_from_flag_clock_and_mark(self, enabled, ran, when, state):
        now = dt.datetime.combine(TODAY, when, tzinfo=IST)
        assert monitor_state(enabled=enabled, monitor_ran=ran, now=now)["state"] == state

    def test_status_bar_monitor_is_idle_on_a_weekend_not_missing(self):
        saturday = dt.datetime(2026, 9, 5, 11, 0, tzinfo=IST)
        assert monitor_state(enabled=True, monitor_ran=False, now=saturday)["state"] == "idle"

    def test_status_bar_refresh_is_five_seconds_only_inside_the_window(self, scenario):
        scenario.config()
        s = scenario.store()
        inside = build_view(s, now=NOW, token={"present": False, "label": "", "expired": True,
                                               "age_minutes": None})
        assert inside["poll_ms"] == 5000 and inside["window_opens_in_ms"] is None
        before = build_view(s, now=NOW.replace(hour=8, minute=0), token=inside["status"]["token"])
        assert before["poll_ms"] == 0 and before["window_opens_in_ms"] == 75 * 60 * 1000
        after = build_view(s, now=NOW.replace(hour=14), token=inside["status"]["token"])
        assert after["poll_ms"] == 0 and after["window_opens_in_ms"] is None


# =======================================================================================
# G2 — a trigger has a Confirm and a countdown; a locked signal has none; SWING prefix
# =======================================================================================
class TestTriggers:
    def test_confirm_button_and_countdown_on_a_triggered_signal(self, client, scenario):
        ids = scenario.morning()
        html = client.get("/swing").text
        triggers = html[html.index('<section id="triggers">'):html.index('<section id="plan">')]
        row = re.search(r'<tr class="sw-trigger sw-trigger-triggered" data-signal-id="%d".*?</tr>'
                        % ids["trigger"], triggers, re.S).group(0)
        assert "5-min ORH 100.50 broken at 100.80" in row
        assert ">Confirm</button>" in row
        forms = _forms(row, "/swing/execute")
        assert len(forms) == 1
        assert f'name="line_id" value="{ids["trigger_line"]}"' in forms[0]
        assert f'name="plan_id" value="{ids["signal_plan"][1]}"' in forms[0]
        assert 'name="confirm" value="true"' in forms[0]
        # built 09:31, page at 09:40 → 21:00 left on a 30-minute plan
        assert re.search(r'class="sw-countdown "[^>]*>21:00<', row)
        assert ids["signal_plan"][1][:8] in row

    def test_confirm_button_is_absent_once_the_plan_has_expired_but_the_row_stays(self, client, scenario, monkeypatch):
        """An expired plan is still shown — with 0:00 and no button — so what was missed is
        visible, and no button can reach a 410."""
        ids = scenario.morning()
        monkeypatch.setattr(swing_desk, "_now", lambda: NOW.replace(hour=10, minute=30))
        html = client.get("/swing").text
        row = re.search(r'data-signal-id="%d".*?</tr>' % ids["trigger"], html, re.S).group(0)
        assert "ALPHAFLAG" in row
        assert ">0:00<" in row and "plan expired" in row
        assert _forms(row, "/swing/execute") == []

    def test_locked_signal_shows_without_a_button(self, client, scenario):
        ids = scenario.morning()
        html = client.get("/swing").text
        row = re.search(r'data-signal-id="%d".*?</tr>' % ids["locked"], html, re.S).group(0)
        assert "GAMMALOCK" in row and "locked at the upper circuit" in row
        assert "Confirm" not in row
        assert _forms(row, "/swing/execute") == []

    def test_locked_and_skipped_triggers_say_why_there_is_no_line(self, client, scenario):
        ids = scenario.morning()
        html = client.get("/swing").text
        row = re.search(r'data-signal-id="%d".*?</tr>' % ids["skipped"], html, re.S).group(0)
        assert "EPSILON" in row and "skipped — TIER_FULL" in row and "2 of 2 positions open" in row
        assert _forms(row, "/swing/execute") == []

    def test_SWING_prefix_on_every_line(self, client, scenario):
        scenario.morning()
        html = client.get("/swing").text
        labels = re.findall(r'title="([^"]+)"', "".join(_forms(html, "/swing/execute")))
        assert labels and all(t.startswith("SWING ") for t in labels)
        for line_label in ("SWING BUY ALPHAFLAG x1666 @ 100.80", "SWING SELL DELTAHELD x100 at open",
                           "SWING RAISE GTT DELTAHELD to 100.80"):
            assert line_label in html

    def test_prefix_and_sizing_line_carry_risk_share_and_cap(self, client, scenario):
        scenario.morning()
        html = client.get("/swing").text
        assert "risk ₹300.00" in html
        assert "1.01% of allocation" in html  # 10,080 of 10,00,000
        assert "cap 20.00%" in html

    def test_triggers_newest_first(self, client, scenario):
        scenario.morning()
        j = client.get("/swing/data").json()
        times = [t["raised_at"][11:16] for t in j["triggers"]]
        assert times == ["09:35", "09:31", "09:20"]


# =======================================================================================
# G5 — no confirm-all: exactly one form per line, one line_id per form
# =======================================================================================
class TestNoConfirmAll:
    def test_one_form_per_line_and_one_line_id_per_form(self, client, scenario):
        ids = scenario.morning()
        html = client.get("/swing").text
        forms = _forms(html, "/swing/execute")
        # the trigger, the SELL, the RAISE, the waiting ALPHAFLAG buy; BETAEP is held → none
        assert len(forms) == 4
        line_ids = []
        for f in forms:
            ids_in_form = re.findall(r'name="line_id" value="(\d+)"', f)
            assert len(ids_in_form) == 1, f
            assert f.count("<button") == 1
            line_ids.append(int(ids_in_form[0]))
        assert sorted(line_ids) == sorted([ids["trigger_line"], ids["sell"], ids["raise"],
                                           ids["waiting"]])
        assert len(set(line_ids)) == len(line_ids)

    def test_no_confirm_all_control_anywhere(self, client, scenario):
        scenario.morning()
        html = client.get("/swing").text
        lowered = html.lower()
        assert "confirm all" not in lowered and "confirm-all" not in lowered.replace("no confirm-all", "")
        assert 'name="line_ids' not in html and 'name="line_id[]' not in html
        assert "select all" not in lowered
        # the only buttons on the page are one per form
        assert html.count("<button") == len(_forms(html, "/swing/execute")) + len(_forms(html, "/swing/rearm"))

    def test_no_confirm_all_in_the_template_source_either(self):
        import pathlib
        src = pathlib.Path("app/templates/swing.html").read_text()
        assert src.count('action="/swing/execute"') == 1  # one macro, one form shape
        assert 'type="checkbox"' not in src


# =======================================================================================
# G3 — the route goes through app.swing_execute.execute_line
# =======================================================================================
class TestExecuteRoute:
    def test_execute_calls_the_execute_module_and_returns_its_outcome(self, client, scenario, fake_execute):
        ids = scenario.morning()
        r = client.post("/swing/execute", data={"plan_id": ids["signal_plan"][1],
                                                "line_id": ids["trigger_line"], "confirm": "true"})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["status"] == "SIMULATED" and j["simulated"] is True
        assert j["line_id"] == ids["trigger_line"] and j["position_id"] == 7
        assert j["gtt"] == {"status": "DRY_RUN_GTT", "trigger": "97.80"}  # Decimal → str
        (call,) = fake_execute
        assert isinstance(call["store"], PgSwingStore) and call["store"].user_id == C.SOLE_USER_ID
        assert call["gw"] == "fake-gateway"
        assert call["now"] == NOW and call["now"].tzinfo is not None
        assert call["plan_id"] == ids["signal_plan"][1] and call["line_id"] == ids["trigger_line"]
        assert call["last_price"] is None  # a buy's entry is its trigger; no broker read
        # the store the route handed over wrote through to the file
        assert scenario.store().line(ids["trigger_line"])["state"] == "FILLED"
        assert scenario.store().session(TODAY)["confirms"] == 2

    def test_execute_of_an_exit_passes_the_brokers_last_price(self, client, scenario, fake_execute):
        """A SELL's simulated fill and a RAISE's stop check need the market; the route reads
        it (a read, not an order) and `execute_line` decides."""
        ids = scenario.morning()
        r = client.post("/swing/execute", data={"plan_id": ids["morning"][1],
                                                "line_id": ids["sell"], "confirm": "true"})
        assert r.status_code == 200
        (call,) = fake_execute
        assert call["last_price"] == Decimal("104.00")
        r = client.post("/swing/rearm", data={"position_id": ids["naked"], "confirm": "true"})
        assert r.status_code == 200
        assert fake_execute[-1]["last_price"] == Decimal("215.00")

    def test_execute_without_a_broker_session_passes_no_price_rather_than_a_guess(self, scenario, monkeypatch):
        monkeypatch.setattr(M, "_kite", None)
        monkeypatch.setattr(C, "KITE_API_KEY", "")  # Kite() refuses to build
        assert swing_desk.last_price("DELTAHELD") is None

    def test_execute_last_price_is_a_read_through_the_kite_wrapper(self, monkeypatch):
        class FakeKite:
            def ltp(self, symbols):
                assert symbols == ["DELTAHELD"]
                return {"DELTAHELD": 104.5}

        monkeypatch.setattr(M, "kite", lambda: FakeKite())
        assert swing_desk.last_price("DELTAHELD") == Decimal("104.5")

    def test_execute_without_confirm_is_400_through_the_route(self, client, scenario, fake_execute):
        ids = scenario.morning()
        r = client.post("/swing/execute", data={"plan_id": ids["signal_plan"][1],
                                                "line_id": ids["trigger_line"], "confirm": "false"})
        assert r.status_code == 400
        assert scenario.store().line(ids["trigger_line"])["state"] == "PROPOSED"

    def test_execute_missing_confirm_field_is_a_validation_error_not_an_order(self, client, scenario, fake_execute):
        ids = scenario.morning()
        r = client.post("/swing/execute", data={"plan_id": ids["signal_plan"][1],
                                                "line_id": ids["trigger_line"]})
        assert r.status_code == 422
        assert fake_execute == []

    def test_execute_of_an_expired_plan_is_410_through_the_route(self, client, scenario, fake_execute, monkeypatch):
        ids = scenario.morning()
        monkeypatch.setattr(swing_desk, "_now", lambda: NOW.replace(hour=10, minute=30))
        r = client.post("/swing/execute", data={"plan_id": ids["signal_plan"][1],
                                                "line_id": ids["trigger_line"], "confirm": "true"})
        assert r.status_code == 410
        assert scenario.store().line(ids["trigger_line"])["state"] == "PROPOSED"

    def test_execute_of_an_unknown_line_is_404_and_a_confirmed_line_409(self, client, scenario, fake_execute):
        ids = scenario.morning()
        r = client.post("/swing/execute", data={"plan_id": ids["signal_plan"][1],
                                                "line_id": 999_999, "confirm": "true"})
        assert r.status_code == 404
        scenario.store().set_line(ids["trigger_line"], state="FILLED")
        r = client.post("/swing/execute", data={"plan_id": ids["signal_plan"][1],
                                                "line_id": ids["trigger_line"], "confirm": "true"})
        assert r.status_code == 409

    def test_execute_route_resolves_the_function_at_call_time(self, client, scenario, exec_module, monkeypatch):
        """Monkeypatching the sibling module's attribute is enough — the route holds no
        reference of its own, so the real `execute_line` is what runs once it exists."""
        ids = scenario.morning()
        seen = []

        async def spy(store, gw, **kw):
            seen.append(kw["line_id"])
            return FakeOutcome("BLOCKED", "spy", None, None, None, True)

        monkeypatch.setattr(exec_module, "execute_line", spy, raising=False)
        r = client.post("/swing/execute", data={"plan_id": ids["signal_plan"][1],
                                                "line_id": ids["trigger_line"], "confirm": "true"})
        assert r.status_code == 200 and r.json()["status"] == "BLOCKED"
        assert seen == [ids["trigger_line"]]

    def test_execute_a_guard_refusal_is_BLOCKED_with_the_guards_words_not_a_500(self, client, scenario, exec_module, monkeypatch):
        """`execute_line` marks the line REJECTED and re-raises an untouchable-instrument
        refusal; the route reports it the way `/execute` does — BLOCKED, with the reason."""
        from app.core.guards import UntouchableInstrumentError

        ids = scenario.morning()

        async def refuse(store, gw, **kw):
            store.set_line(kw["line_id"], state="REJECTED")
            raise UntouchableInstrumentError("SGBDE31III is untouchable")

        monkeypatch.setattr(exec_module, "execute_line", refuse, raising=False)
        monkeypatch.setattr(exec_module, "swing_gates",
                            lambda: types.SimpleNamespace(dry_run=True), raising=False)
        r = client.post("/swing/execute", data={"plan_id": ids["signal_plan"][1],
                                                "line_id": ids["trigger_line"], "confirm": "true"})
        assert r.status_code == 200
        j = r.json()
        assert j["status"] == "BLOCKED" and "untouchable" in j["reason"]
        assert j["line_id"] == ids["trigger_line"] and j["simulated"] is True
        assert scenario.store().line(ids["trigger_line"])["state"] == "REJECTED"

    def test_execute_rearm_route_goes_the_same_way(self, client, scenario, fake_execute):
        ids = scenario.morning()
        r = client.post("/swing/rearm", data={"position_id": ids["naked"], "confirm": "true"})
        assert r.status_code == 200 and r.json()["status"] == "SIMULATED"
        assert r.json()["position_id"] == ids["naked"]
        r = client.post("/swing/rearm", data={"position_id": ids["held"], "confirm": "true"})
        assert r.json()["status"] == "BLOCKED"
        r = client.post("/swing/rearm", data={"position_id": ids["naked"], "confirm": "no"})
        assert r.status_code == 400

    def test_execute_rearm_form_exists_only_for_the_naked_position(self, client, scenario):
        ids = scenario.morning()
        html = client.get("/swing").text
        forms = _forms(html, "/swing/rearm")
        assert len(forms) == 1
        assert f'name="position_id" value="{ids["naked"]}"' in forms[0]

    def test_execute_route_needs_a_same_origin_post(self, scenario, monkeypatch):
        """The desk's websec middleware covers the new route as it covers /execute."""
        monkeypatch.setattr(swing_desk, "open_store",
                            lambda: contextlib.nullcontext(scenario.store()))
        ids = scenario.morning()
        c = TestClient(M.app, headers={"Origin": "http://evil.example"})
        r = c.post("/swing/execute", data={"plan_id": ids["signal_plan"][1],
                                           "line_id": ids["trigger_line"], "confirm": "true"})
        assert r.status_code == 403


# =======================================================================================
# G3, through the real module once it exists: the route, the real execute_line, a real
# gateway in dry-run over a broker client that explodes on contact, and the sqlite store.
# =======================================================================================
class ExplodingKC:
    """A broker client no test may touch. Every method that could reach Zerodha explodes."""

    VARIETY_REGULAR = "regular"
    TRANSACTION_TYPE_BUY, TRANSACTION_TYPE_SELL = "BUY", "SELL"
    PRODUCT_CNC, ORDER_TYPE_LIMIT, ORDER_TYPE_MARKET = "CNC", "LIMIT", "MARKET"
    VALIDITY_DAY, GTT_TYPE_SINGLE = "DAY", "single"

    def place_order(self, **_):
        raise AssertionError("an order reached the broker")

    def place_gtt(self, **_):
        raise AssertionError("a GTT reached the broker")

    def delete_gtt(self, *_, **__):
        raise AssertionError("a GTT delete reached the broker")

    def __getattr__(self, name):
        raise AssertionError(f"kc.{name} reached the broker")


real_execute = pytest.importorskip("app.swing_execute", reason="leaf 1.1.1 has not landed yet")


@pytest.fixture()
def real_client(scenario, monkeypatch, tmp_path):
    from app.core.risk import RiskManager

    monkeypatch.setattr(C, "SWING_EXECUTION_ENABLED", False)
    monkeypatch.setattr(C, "DRY_RUN", True)
    monkeypatch.setattr(M, "_kite", None)
    monkeypatch.setattr(swing_desk, "open_store",
                        lambda: contextlib.nullcontext(scenario.store()))
    monkeypatch.setattr(swing_desk, "_now", lambda: NOW)
    monkeypatch.setattr(swing_desk, "last_price", lambda symbol: LAST_PRICES.get(symbol))
    monkeypatch.setattr(C, "TOKEN_FILE", str(tmp_path / "no-such-token.json"))
    gw = real_execute.build_swing_gateway(ExplodingKC(), RiskManager())
    monkeypatch.setattr(swing_desk, "_swing_gateway", gw)
    return TestClient(M.app)


class TestExecuteThroughTheRealModule:
    def test_execute_a_buy_end_to_end_is_simulated_and_the_book_is_written(self, real_client, scenario):
        ids = scenario.morning()
        r = real_client.post("/swing/execute", data={"plan_id": ids["signal_plan"][1],
                                                     "line_id": ids["trigger_line"],
                                                     "confirm": "true"})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["status"] == "SIMULATED" and j["simulated"] is True and j["reason"] == ""
        assert j["line_id"] == ids["trigger_line"] and j["position_id"]
        assert j["order"]["status"] == "DRY_RUN" and j["gtt"]["status"] == "DRY_RUN_GTT"
        s = scenario.store()
        line = s.line(ids["trigger_line"])
        assert line["state"] == "FILLED" and line["position_id"] == j["position_id"]
        pos = s.position(j["position_id"])
        assert pos["symbol"] == "ALPHAFLAG" and pos["quantity_open"] == 1666
        assert pos["entry_avg"] == Decimal("100.80") and pos["stop"] == Decimal("97.80")
        assert pos["gtt_id"].startswith("DRY-") and pos["simulated"] is True
        assert pos["gtt_armed_at"] == NOW
        (fill,) = s.fills_for(j["position_id"])
        assert fill["side"] == "BUY" and fill["quantity"] == 1666 and fill["simulated"] is True
        session = s.session(TODAY)
        assert session["confirms"] == 2 and session["fills"] == 2  # the scenario had 1 and 1
        # and the page now shows the position, armed, and the line as done
        html = real_client.get("/swing").text
        assert "ALPHAFLAG" in html[html.index('<section id="book">'):]
        row = re.search(r'data-signal-id="%d".*?</tr>' % ids["trigger"], html, re.S).group(0)
        assert "filled" in row and _forms(row, "/swing/execute") == []

    def test_execute_without_confirm_is_400_through_the_real_module(self, real_client, scenario):
        ids = scenario.morning()
        r = real_client.post("/swing/execute", data={"plan_id": ids["signal_plan"][1],
                                                     "line_id": ids["trigger_line"],
                                                     "confirm": "yes"})
        assert r.status_code == 400
        assert scenario.store().line(ids["trigger_line"])["state"] == "PROPOSED"

    def test_execute_expired_is_410_through_the_real_module_and_marks_the_line(self, real_client, scenario, monkeypatch):
        ids = scenario.morning()
        monkeypatch.setattr(swing_desk, "_now", lambda: NOW.replace(hour=10, minute=30))
        r = real_client.post("/swing/execute", data={"plan_id": ids["signal_plan"][1],
                                                     "line_id": ids["trigger_line"],
                                                     "confirm": "true"})
        assert r.status_code == 410
        assert scenario.store().line(ids["trigger_line"])["state"] == "EXPIRED"

    def test_execute_a_sell_partial_through_the_real_module(self, real_client, scenario):
        ids = scenario.morning()
        r = real_client.post("/swing/execute", data={"plan_id": ids["morning"][1],
                                                     "line_id": ids["sell"], "confirm": "true"})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["status"] == "SIMULATED", j
        pos = scenario.store().position(ids["held"])
        assert pos["quantity_open"] == 200 and pos["state"] == "PARTIAL"
        assert pos["partial_done"] is True and pos["exit_avg"] == Decimal("104.0000")

    def test_execute_rearm_through_the_real_module(self, real_client, scenario):
        ids = scenario.morning()
        r = real_client.post("/swing/rearm", data={"position_id": ids["naked"], "confirm": "true"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "SIMULATED"
        pos = scenario.store().position(ids["naked"])
        assert pos["gtt_id"] is not None and pos["gtt_trigger"] == Decimal("204.50")
        # a second re-arm is BLOCKED: two triggers would sell twice what is held
        r = real_client.post("/swing/rearm", data={"position_id": ids["naked"], "confirm": "true"})
        assert r.json()["status"] == "BLOCKED"

    def test_execute_no_order_reached_the_broker_in_this_class(self, real_client, scenario):
        """`ExplodingKC` would have raised into a 500; every 200 above is the proof."""
        ids = scenario.morning()
        r = real_client.post("/swing/execute", data={"plan_id": ids["morning"][1],
                                                     "line_id": ids["raise"], "confirm": "true"})
        assert r.status_code == 200 and r.json()["status"] == "SIMULATED"


# =======================================================================================
# G6 — the page module never names a placing verb or the broker client
# =======================================================================================
def _code_only(source: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.body and isinstance(node.body[0], ast.Expr) and isinstance(
                getattr(node.body[0], "value", None), ast.Constant
            ):
                node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(tree)


class TestTheModuleIsAPage:
    def test_execute_verbs_and_the_broker_are_never_named(self):
        source = _code_only(inspect.getsource(swing_desk))
        for forbidden in ("kc.place_order", "place_gtt_stop(", "delete_gtt(", "gw.place(",
                          "kiteconnect", ".place(", "place_order"):
            assert forbidden not in source, forbidden

    def test_the_gateway_is_built_lazily_never_at_import(self):
        assert swing_desk._swing_gateway is None
        source = _code_only(inspect.getsource(swing_desk))
        assert "build_swing_gateway" in source  # only inside swing_gateway()


# =======================================================================================
# G4 — PgSwingStore round-trips every SwingStore method against sqlite
# =======================================================================================
class TestStore:
    def test_store_plan_and_line_round_trip_with_decimals_and_tz_aware_stamps(self, scenario, store):
        ids = scenario.morning()
        plan = store.plan(ids["signal_plan"][1])
        assert plan["id"] == ids["signal_plan"][0] and plan["plan_id"] == ids["signal_plan"][1]
        assert plan["as_of"] == TODAY and plan["source"] == "SIGNAL" and plan["gate"] == "GREEN"
        assert plan["built_at"] == dt.datetime(2026, 9, 2, 9, 31, tzinfo=IST)
        assert plan["expires_at"] == plan["built_at"] + dt.timedelta(minutes=30)
        assert plan["expires_at"].tzinfo is not None
        assert plan["exposure_level"] == 2
        assert store.plan(str(uuid.uuid4())) is None
        assert store.plan("not-a-uuid") is None  # a uuid column on Postgres: no driver error

        line = store.line(ids["trigger_line"])
        assert line["plan_pk"] == plan["id"] and line["plan_id"] == plan["plan_id"]
        assert line["kind"] == "BUY_ON_TRIGGER" and line["symbol"] == "ALPHAFLAG"
        assert line["instrument_id"] == 1 and line["quantity"] == 1666
        assert isinstance(line["trigger"], Decimal) and line["trigger"] == Decimal("100.80")
        assert isinstance(line["stop"], Decimal) and line["stop"] == Decimal("97.80")
        assert line["risk_inr"] == Decimal("300.00") and line["position_value"] == Decimal("10080.00")
        assert line["state"] == "PROPOSED" and line["trail"] == "MA10"
        assert line["client_id"] == f"{plan['plan_id']}:ALPHAFLAG:BUY_ON_TRIGGER"
        assert store.line(424242) is None

    def test_store_line_without_prices_returns_none_not_zero(self, scenario, store):
        ids = scenario.morning()
        sell = store.line(ids["sell"])
        assert sell["trigger"] is None and sell["stop"] is None and sell["kind"] == "SELL_AT_OPEN"

    def test_store_set_line_keeps_what_it_was_not_told_to_change(self, scenario, store):
        ids = scenario.morning()
        store.set_line(ids["trigger_line"], state="SENT", journal_ref="order-1")
        line = store.line(ids["trigger_line"])
        assert (line["state"], line["journal_ref"], line["position_id"]) == ("SENT", "order-1", None)
        store.set_line(ids["trigger_line"], state="FILLED", position_id=9)
        line = store.line(ids["trigger_line"])
        assert (line["state"], line["journal_ref"], line["position_id"]) == ("FILLED", "order-1", 9)
        # both at once, as `_record_line` does — the two must land in their own columns
        store.set_line(ids["sell"], state="FILLED", journal_ref="DRY-abc:X:BUY", position_id=4)
        line = store.line(ids["sell"])
        assert (line["journal_ref"], line["position_id"]) == ("DRY-abc:X:BUY", 4)

    def test_store_positions_round_trip_and_open_position_for(self, scenario, store):
        scenario.config()
        pid = store.create_position({
            "instrument_id": 1, "setup": "FLAG", "entry_date": TODAY,
            "entry_avg": Decimal("100.8"), "quantity_entered": 1666,
            "initial_stop": Decimal("97.8"), "stop": Decimal("97.8"), "trail": "MA10",
            "gtt_id": "G-1", "gtt_trigger": Decimal("97.80"), "gtt_armed_at": NOW,
            "simulated": True,
        })
        pos = store.position(pid)
        assert pos["id"] == pid and pos["symbol"] == "ALPHAFLAG" and pos["state"] == "OPEN"
        assert pos["quantity_open"] == 1666  # defaulted to quantity_entered
        assert isinstance(pos["entry_avg"], Decimal) and pos["entry_avg"] == Decimal("100.8000")
        assert pos["stop"] == Decimal("97.80") and pos["initial_stop"] == Decimal("97.80")
        assert pos["gtt_armed_at"] == NOW and pos["entry_date"] == TODAY
        assert pos["simulated"] is True and pos["partial_done"] is False
        assert pos["closed_on"] is None and pos["r_multiple"] is None
        assert store.open_position_for(1)["id"] == pid
        assert store.open_position_for(2) is None
        assert store.position(999) is None

        store.update_position(pid, {"quantity_open": 1111, "partial_done": True,
                                    "partial_date": TODAY, "state": "PARTIAL",
                                    "stop": Decimal("100.80"), "gtt_id": None, "gtt_trigger": None})
        pos = store.position(pid)
        assert pos["quantity_open"] == 1111 and pos["state"] == "PARTIAL"
        assert pos["partial_done"] is True and pos["partial_date"] == TODAY
        assert pos["stop"] == Decimal("100.80") and pos["gtt_id"] is None
        assert store.open_position_for(1)["id"] == pid  # PARTIAL with shares still counts

        store.update_position(pid, {"quantity_open": 0, "state": "CLOSED", "closed_on": TODAY,
                                    "exit_avg": Decimal("104.25"), "close_reason": "MANUAL",
                                    "r_multiple": Decimal("1.15"), "pnl_inr": Decimal("5748.7")})
        pos = store.position(pid)
        assert pos["state"] == "CLOSED" and pos["exit_avg"] == Decimal("104.2500")
        assert pos["r_multiple"] == Decimal("1.15") and pos["pnl_inr"] == Decimal("5748.70")
        assert store.open_position_for(1) is None

    def test_store_rounds_at_write_time(self, scenario, store):
        """House rule 8: the stored number is the contract. A stop with a third decimal is
        rounded to the column's scale before it is written, not on the way back out."""
        scenario.config()
        pid = store.create_position({
            "instrument_id": 1, "setup": "FLAG", "entry_date": TODAY,
            "entry_avg": Decimal("100.123456"), "quantity_entered": 10,
            "initial_stop": Decimal("97.805"), "stop": Decimal("97.805"), "trail": "MA20",
        })
        raw = scenario.conn.execute("SELECT entry_avg, stop FROM sw_position WHERE id = ?",
                                    (pid,)).fetchone()
        assert Decimal(str(raw["entry_avg"])) == Decimal("100.1235")
        assert Decimal(str(raw["stop"])) == Decimal("97.81")

    def test_store_refuses_a_column_it_does_not_know(self, scenario, store):
        scenario.config()
        with pytest.raises(ValueError):
            store.create_position({"instrument_id": 1, "gtt": "no"})
        with pytest.raises(ValueError):
            store.update_position(1, {"gtt": "no"})

    def test_store_create_position_accepts_the_symbol_execute_line_passes(self, scenario, store):
        """`execute_line` hands the line's symbol along with the row; the table keys the
        instrument by id, so the symbol is dropped rather than refused."""
        scenario.config()
        pid = store.create_position({
            "instrument_id": 2, "symbol": "BETAEP", "setup": "EP", "entry_date": TODAY,
            "entry_avg": Decimal("210.50"), "quantity_entered": 50, "quantity_open": 50,
            "initial_stop": Decimal("204.50"), "stop": Decimal("204.50"), "trail": "MA20",
            "gtt_id": None, "gtt_trigger": None, "gtt_armed_at": None, "partial_done": False,
            "partial_date": None, "state": "OPEN", "closed_on": None, "exit_avg": None,
            "close_reason": None, "r_multiple": None, "pnl_inr": None, "simulated": True,
        })
        assert store.position(pid)["symbol"] == "BETAEP"

    def test_store_add_fill_round_trips(self, scenario, store):
        scenario.config()
        pid = scenario.position(instrument_id=1)
        fid = store.add_fill({"position_id": pid, "side": "BUY", "quantity": 300,
                              "price": Decimal("100.80"), "filled_at": NOW,
                              "journal_ref": "sim-1", "simulated": True})
        (fill,) = store.fills_for(pid)
        assert fill["id"] == fid and fill["side"] == "BUY" and fill["quantity"] == 300
        assert isinstance(fill["price"], Decimal) and fill["price"] == Decimal("100.8000")
        assert fill["filled_at"] == NOW and fill["simulated"] is True
        with pytest.raises(ValueError):
            store.add_fill({"position_id": pid, "side": "BUY", "qty": 1})

    def test_store_bump_session_upserts_and_adds(self, scenario, store):
        assert store.session(TODAY) is None
        store.bump_session(TODAY, mode="DRY_RUN", confirms=1)
        row = store.session(TODAY)
        assert row["mode"] == "DRY_RUN" and row["confirms"] == 1 and row["fills"] == 0
        assert row["monitor_ran"] is False and row["session_date"] == TODAY
        store.bump_session(TODAY, mode="DRY_RUN", confirms=1, fills=1, manage_actions=2)
        row = store.session(TODAY)
        assert (row["confirms"], row["fills"], row["manage_actions"]) == (2, 1, 2)
        assert scenario.conn.execute("SELECT count(*) FROM sw_session").fetchone()[0] == 1

    def test_store_bump_session_onto_the_monitors_row_keeps_its_mark(self, scenario, store):
        scenario.session(monitor_ran=True, signals=4)
        store.bump_session(TODAY, mode="DRY_RUN", confirms=1, fills=1)
        row = store.session(TODAY)
        assert row["monitor_ran"] is True and row["signals"] == 4
        assert row["confirms"] == 1 and row["fills"] == 1

    def test_store_config_and_first_live_sessions(self, scenario, store):
        scenario.config(capital="250000.00", risk="0.750", first_live=3)
        cfg = store.config()
        assert cfg["sleeve_capital_inr"] == Decimal("250000.00")
        assert isinstance(cfg["sleeve_capital_inr"], Decimal)
        assert cfg["risk_per_trade_pct"] == Decimal("0.750")
        assert cfg["first_live_sessions_left"] == 3 and cfg["exposure_level"] == 0
        store.set_first_live_sessions_left(2)
        assert store.config()["first_live_sessions_left"] == 2
        store.set_first_live_sessions_left(-1)
        assert store.config()["first_live_sessions_left"] == 0  # never below the CHECK

    def test_store_config_without_a_row_is_the_schema_default(self, store):
        cfg = store.config()
        assert cfg["sleeve_capital_inr"] == Decimal(0) and cfg["first_live_sessions_left"] == 5
        assert cfg["risk_per_trade_pct"] == Decimal("0.5") and cfg["present"] is False

    def test_store_is_scoped_to_its_user(self, scenario):
        ids = scenario.morning()
        other = PgSwingStore(_connect(scenario.path), user_id=2, schema="")
        assert other.plan(ids["signal_plan"][1]) is None
        assert other.line(ids["trigger_line"]) is None
        assert other.position(ids["held"]) is None
        assert other.open_positions() == [] and other.signals_for(TODAY) == []

    def test_store_schema_prefix_qualifies_every_table(self, scenario):
        """On the desk's Postgres the connection is on search_path=desk and the swing book is
        in public. Every statement the store issues must carry the prefix."""
        issued: list[str] = []

        class Spy:
            def execute(self, sql, params=()):
                issued.append(sql)
                return scenario.conn.execute(sql.replace("public.", ""), params)

        s = PgSwingStore(Spy(), user_id=USER, schema="public")
        scenario.config()
        s.config(); s.plan("x"); s.line(1); s.position(1); s.open_position_for(1)
        s.session(TODAY); s.signals_for(TODAY); s.latest_plan("MORNING"); s.open_positions()
        s.recent_manage_actions(); s.skips_for(1); s.lines_for(1); s.signal_skip_for(1, TODAY)
        s.bump_session(TODAY, mode="DRY_RUN"); s.set_first_live_sessions_left(4)
        s.set_line(1, state="EXPIRED")
        assert issued
        for sql in issued:
            for table in re.findall(r"\b(?:FROM|JOIN|INTO|UPDATE)\s+(?!SET\b)([A-Za-z_.]+)", sql):
                assert table.startswith("public."), sql
        assert all("?" in sql for sql in issued)  # `?` placeholders, the adapter's contract

    def test_store_open_positions_lead_with_the_naked_one(self, scenario, store):
        ids = scenario.morning()
        rows = store.open_positions()
        assert [r["id"] for r in rows] == [ids["naked"], ids["held"]]
        assert rows[0]["gtt_id"] is None and rows[1]["gtt_id"] == "G-44"


# =======================================================================================
# The view, directly: the judgement calls a rendered page hides
# =======================================================================================
class TestView:
    def test_a_waiting_buy_for_a_name_already_held_gets_no_button(self, scenario, store):
        ids = scenario.morning()
        v = build_view(store, now=NOW, token={"present": False, "label": "", "expired": True,
                                              "age_minutes": None})
        buys = {b["id"]: b for b in v["plans"]["morning"]["buys"]}
        assert buys[ids["waiting"]]["confirmable"] is True
        held = buys[ids["waiting_held"]]
        assert held["confirmable"] is False and held["why_not"].startswith("already held")

    def test_an_exit_for_a_name_the_book_does_not_hold_gets_no_button(self, scenario, store):
        scenario.config()
        pk, pid = scenario.plan(source="MORNING", built_at=NOW - dt.timedelta(minutes=5))
        lid = scenario.line(pk, pid, kind="SELL_AT_OPEN", instrument_id=5, symbol="EPSILON",
                            quantity=10, trigger=None, stop=None)
        v = build_view(store, now=NOW, token={"present": False, "label": "", "expired": True,
                                              "age_minutes": None})
        (exit_line,) = v["plans"]["morning"]["exits"]
        assert exit_line["id"] == lid and exit_line["confirmable"] is False
        assert exit_line["why_not"] == "not a swing position"

    def test_a_sell_for_more_than_open_and_a_raise_below_the_stop_get_no_button(self, scenario, store):
        scenario.config()
        scenario.position(instrument_id=4, quantity=300, quantity_open=100, stop="100.00",
                          initial_stop="97.80")
        pk, pid = scenario.plan(source="MORNING", built_at=NOW - dt.timedelta(minutes=5))
        big = scenario.line(pk, pid, kind="SELL_AT_OPEN", instrument_id=4, symbol="DELTAHELD",
                            quantity=150, trigger=None, stop=None)
        low = scenario.line(pk, pid, kind="RAISE_GTT_STOP", instrument_id=4, symbol="DELTAHELD",
                            quantity=0, trigger=None, stop="99.00")
        v = build_view(store, now=NOW, token={"present": False, "label": "", "expired": True,
                                              "age_minutes": None})
        by_id = {e["id"]: e for e in v["plans"]["morning"]["exits"]}
        assert by_id[big]["confirmable"] is False and "more than the 100 open" in by_id[big]["why_not"]
        assert by_id[low]["confirmable"] is False and "resting stop" in by_id[low]["why_not"]

    def test_yesterdays_morning_plan_is_not_todays(self, scenario, store):
        scenario.config()
        scenario.plan(source="MORNING", built_at=dt.datetime(2026, 9, 1, 9, 10, tzinfo=IST),
                      as_of=dt.date(2026, 9, 1))
        v = build_view(store, now=NOW, token={"present": False, "label": "", "expired": True,
                                              "age_minutes": None})
        assert v["plans"]["morning"] is None

    def test_the_eod_preview_is_shown_expired_with_its_lines_and_no_buttons(self, scenario, store):
        scenario.morning()
        v = build_view(store, now=NOW, token={"present": False, "label": "", "expired": True,
                                              "age_minutes": None})
        preview = v["plans"]["preview"]
        assert preview["source"] == "EOD_PREVIEW" and preview["expired"] is True
        assert preview["countdown"] == "0:00"
        assert preview["exits"] and all(not e["confirmable"] for e in preview["exits"])

    def test_the_fingerprint_moves_when_a_line_moves(self, scenario, store):
        ids = scenario.morning()
        token = {"present": False, "label": "", "expired": True, "age_minutes": None}
        before = build_view(store, now=NOW, token=token)["fingerprint"]
        assert before == build_view(store, now=NOW, token=token)["fingerprint"]
        store.set_line(ids["trigger_line"], state="FILLED")
        assert build_view(store, now=NOW, token=token)["fingerprint"] != before

    def test_the_view_needs_a_tz_aware_clock(self, scenario, store):
        with pytest.raises(ValueError):
            build_view(store, now=dt.datetime(2026, 9, 2, 9, 40))


# =======================================================================================
# G7 — the nav carries Swing and every page links to it
# =======================================================================================
class TestNav:
    def test_nav_links_to_swing_from_every_desk_page_and_back(self, client, scenario):
        scenario.config()
        routes = ["/", "/performance", "/regime", "/regime/backtest", "/tradebook", "/settings",
                  "/swing"]
        for page in routes:
            r = client.get(page)
            assert r.status_code == 200, page
            links = set(re.findall(r'href="([^"]+)"', r.text))
            assert "/swing" in links, f"{page} does not link to /swing"
            if page == "/swing":
                missing = [x for x in routes if x not in links]
                assert missing == [], f"/swing does not link to {missing}"

    def test_nav_entry_sits_after_reconcile(self):
        import pathlib
        shell = pathlib.Path("app/templates/base.html").read_text()
        assert shell.index("('/reconcile', 'Reconcile')") < shell.index("('/swing', 'Swing')")
        assert shell.index("('/swing', 'Swing')") < shell.index("('/ops', 'Operations')")

    def test_nav_marks_swing_current_on_its_own_page(self, client, scenario):
        scenario.config()
        assert 'href="/swing" aria-current="page"' in client.get("/swing").text
