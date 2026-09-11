"""TW6/TW6a — the three-weeks-tight desk page, its store, and the six routes the runbook needs.

What is asserted, and where it comes from:

* ``docs/twt/05`` §2: the session strip, **exits first**, then entries with their skips, then
  the book with a Re-arm button per naked line, then Confirm per line. An expired plan's buttons
  are **gone, not disabled**. The 15:15 strip names every open line without a resting GTT.
* ``docs/twt/FIRST-LIVE-MORNING.md`` §11's inventory: ``/twt``, ``/twt/data``, ``/twt/execute``,
  ``/twt/halt``, ``/twt/rearm``, ``/twt/sweep`` and ``/twt/reconcile`` all exist, to those exact
  spellings, because the runbook was written before the code and names them.
* §7's four behaviours of ``/twt/halt``, **including the third**: it never touches protection.
* §1's two details that would cost a morning: the desk's basic auth ignores the username, and
  **every POST needs an ``Origin`` header**. A POST without one is a 403 that reads exactly like
  a permissions problem and is not.
* The ``TwtStore`` protocol: every method round-trips against sqlite with ``Decimal`` in and out.

THE DATABASE
------------
The ``tw_`` tables are built here from a DDL that mirrors ``alembic/versions/0041_twt.py`` for
the columns the store reads, in sqlite's spelling, on a per-test file — and the store runs the
same SQL it runs on Postgres with the schema prefix set to ``""``. A desk suite cannot depend on
a Postgres being up, and a fake connection would prove nothing about the SQL.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import re
import sqlite3
import uuid
from decimal import Decimal

import pytest

from app import config as C
from app import main as M
from app import twt_desk as W
from app import twt_execute as X

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
NOW = dt.datetime(2026, 9, 11, 9, 20, tzinfo=IST)
SESSION = dt.date(2026, 9, 10)
D = Decimal

DDL = """
CREATE TABLE instrument (id INTEGER PRIMARY KEY, symbol TEXT);
CREATE TABLE ohlcv_daily (
  instrument_id INTEGER, date TEXT, close NUMERIC, adj_factor NUMERIC DEFAULT 1,
  PRIMARY KEY (instrument_id, date));
CREATE TABLE tw_config (
  user_id INTEGER PRIMARY KEY, sleeve_capital_inr NUMERIC, max_open_positions INTEGER,
  max_position_pct NUMERIC, stop_pct NUMERIC, trail_pct NUMERIC,
  first_live_entries_left INTEGER, dry_run_sessions INTEGER, updated_at TEXT, updated_by TEXT);
CREATE TABLE tw_config_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, key TEXT, old_value TEXT,
  new_value TEXT, changed_at TEXT, changed_by TEXT, note TEXT);
CREATE TABLE tw_breadth_daily (
  user_id INTEGER, date TEXT, universe_count INTEGER, measured_count INTEGER,
  above_count INTEGER, pct_above_dma NUMERIC, gate TEXT, dma_bars INTEGER,
  thin_session INTEGER DEFAULT 0, PRIMARY KEY (user_id, date));
CREATE TABLE tw_signal_daily (
  user_id INTEGER, date TEXT, instrument_id INTEGER, state TEXT,
  entry_reference_close NUMERIC, stop_preview NUMERIC, sessions_out_before INTEGER,
  rank_key INTEGER, turnover_avg_20 INTEGER, PRIMARY KEY (user_id, date, instrument_id));
CREATE TABLE tw_plan (
  id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT, user_id INTEGER, session_date TEXT,
  source TEXT, built_at TEXT, expires_at TEXT, plan_hash TEXT, gate TEXT,
  sleeve_equity_inr NUMERIC, total_new_exposure_inr NUMERIC);
CREATE TABLE tw_plan_line (
  id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER, user_id INTEGER, instrument_id INTEGER,
  kind TEXT, state TEXT, quantity INTEGER, stop_price NUMERIC, value_inr NUMERIC,
  high_since NUMERIC, previous_trigger NUMERIC, note TEXT, client_id TEXT, journal_ref TEXT,
  order_id INTEGER, position_id INTEGER);
CREATE TABLE tw_plan_skip (
  id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER, user_id INTEGER, instrument_id INTEGER,
  reason TEXT, detail TEXT);
CREATE TABLE tw_position (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, broker_account_id INTEGER,
  instrument_id INTEGER, order_id INTEGER, signal_date TEXT, entry_date TEXT,
  entry_avg NUMERIC, entry_adj_factor NUMERIC DEFAULT 1, quantity_entered INTEGER,
  quantity_open INTEGER, initial_stop NUMERIC, stop_price NUMERIC, high_since NUMERIC,
  high_since_date TEXT, gtt_id TEXT, gtt_trigger NUMERIC, gtt_armed_at TEXT,
  next_trigger NUMERIC, next_trigger_for TEXT, state TEXT, closed_on TEXT, exit_avg NUMERIC,
  close_reason TEXT, pnl_inr NUMERIC, return_pct NUMERIC, simulated INTEGER DEFAULT 1,
  half_size INTEGER DEFAULT 0);
CREATE TABLE tw_order (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, broker_account_id INTEGER,
  instrument_id INTEGER, signal_date TEXT, side TEXT, quantity INTEGER, stop_price NUMERIC,
  state TEXT, broker_order_id TEXT, client_id TEXT, filled_quantity INTEGER DEFAULT 0,
  avg_fill_price NUMERIC, position_id INTEGER, simulated INTEGER DEFAULT 1);
CREATE TABLE tw_fill (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, position_id INTEGER, order_id INTEGER,
  side TEXT, quantity INTEGER, price NUMERIC, filled_at TEXT, journal_ref TEXT,
  simulated INTEGER DEFAULT 1);
CREATE TABLE tw_session (
  user_id INTEGER, session_date TEXT, mode TEXT DEFAULT 'DRY_RUN', gate TEXT DEFAULT 'SHUT',
  states INTEGER DEFAULT 0, signals INTEGER DEFAULT 0, confirms INTEGER DEFAULT 0,
  fills INTEGER DEFAULT 0, ratchets INTEGER DEFAULT 0, exits INTEGER DEFAULT 0,
  naked_at_1515 INTEGER DEFAULT 0, first_live_entries_counted INTEGER DEFAULT 0,
  PRIMARY KEY (user_id, session_date));
"""


#: sqlite3 has no ``Decimal``. The desk's Postgres driver does, so this is a *test* adapter and
#: not a shim in the store: money crosses this boundary as the string of its exact decimal,
#: which is the same thing house rule 9 asks of every other surface.
sqlite3.register_adapter(Decimal, str)
#: And no ``datetime`` either, in a spelling that sorts. Python's default adapter renders
#: ``2026-09-11 09:20:00+05:30`` with a space where ``isoformat()`` writes a ``T``, and sqlite
#: compares timestamps as text — so a bound ``now`` would sort *before* every stored
#: ``expires_at`` and "which plans are still live" would answer "all of them". Postgres compares
#: real timestamps and has no such hazard; this keeps the two sides of the test agreeing.
sqlite3.register_adapter(dt.datetime, lambda value: value.isoformat())


@pytest.fixture
def store(tmp_path):  # noqa: ANN001, ANN201 - the desk's own fixture idiom
    # `check_same_thread=False` because `TestClient` runs the app in a worker thread while the
    # test holds the connection; the desk's own Postgres pool has no such constraint.
    conn = sqlite3.connect(tmp_path / "twt.db", isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    conn.execute("INSERT INTO instrument (id, symbol) VALUES (42, 'TWTCO'), (43, 'HELDCO')")
    conn.execute(
        "INSERT INTO tw_config (user_id, sleeve_capital_inr, max_open_positions, "
        "max_position_pct, stop_pct, trail_pct, first_live_entries_left, dry_run_sessions, "
        "updated_at, updated_by) "
        "VALUES (1, '2500000.00', 10, '12.50', '20.00', '20.00', 10, 0, ?, 'test')",
        (NOW.isoformat(),),
    )
    conn.execute(
        "INSERT INTO tw_breadth_daily (user_id, date, universe_count, measured_count, "
        "above_count, pct_above_dma, gate, dma_bars, thin_session) "
        "VALUES (1, ?, 4000, 1200, 700, '58.3333', 'OPEN', 200, 0)",
        (SESSION.isoformat(),),
    )
    conn.execute(
        "INSERT INTO ohlcv_daily (instrument_id, date, close, adj_factor) "
        "VALUES (42, ?, '100.00', 1), (43, ?, '100.00', 1)",
        (SESSION.isoformat(), SESSION.isoformat()),
    )
    conn.execute(
        "INSERT INTO tw_signal_daily (user_id, date, instrument_id, state, "
        "entry_reference_close, stop_preview, sessions_out_before, rank_key, turnover_avg_20) "
        "VALUES (1, ?, 42, 'SIGNAL', '100.00', '80.00', 5, 500000000, 500000000)",
        (SESSION.isoformat(),),
    )
    return W.PgTwtStore(conn, user_id=1, schema="", broker_account_id=1)


def a_plan(store, *, expires_in_minutes: int = 25, source: str = "MORNING"):  # noqa: ANN001, ANN201
    plan_id = str(uuid.uuid4())
    store.conn.execute(
        "INSERT INTO tw_plan (plan_id, user_id, session_date, source, built_at, expires_at, "
        "plan_hash, gate, sleeve_equity_inr, total_new_exposure_inr) "
        "VALUES (?, 1, ?, ?, ?, ?, 'abc', 'OPEN', '2500000.00', '250000.00')",
        (
            plan_id,
            SESSION.isoformat(),
            source,
            (NOW - dt.timedelta(minutes=5)).isoformat(),
            (NOW + dt.timedelta(minutes=expires_in_minutes)).isoformat(),
        ),
    )
    plan_pk = store.conn.execute(
        "SELECT id FROM tw_plan WHERE plan_id = ?", (plan_id,)
    ).fetchone()["id"]
    return plan_id, int(plan_pk)


def a_line(  # noqa: PLR0913 - a plan line is its columns
    store,  # noqa: ANN001
    plan_pk: int,
    plan_id: str,
    *,
    kind: str = "BUY_AT_OPEN",
    instrument_id: int = 42,
    symbol: str = "TWTCO",
    quantity: int = 2_500,
    stop: str = "80.00",
    value: str = "250000.00",
    previous: str | None = None,
    high_since: str | None = None,
) -> int:
    suffix = "" if kind == "BUY_AT_OPEN" else f":{kind}"
    store.conn.execute(
        "INSERT INTO tw_plan_line (plan_id, user_id, instrument_id, kind, state, quantity, "
        "stop_price, value_inr, high_since, previous_trigger, note, client_id) "
        "VALUES (?, 1, ?, ?, 'PROPOSED', ?, ?, ?, ?, ?, '', ?)",
        (
            plan_pk,
            instrument_id,
            kind,
            quantity,
            stop,
            value,
            high_since,
            previous,
            f"{plan_id}:{symbol}{suffix}",
        ),
    )
    return int(store.conn.execute("SELECT MAX(id) AS id FROM tw_plan_line").fetchone()["id"])


def a_position(  # noqa: PLR0913 - a book row is its numbers
    store,  # noqa: ANN001
    *,
    instrument_id: int = 43,
    quantity: int = 1_000,
    entry: str = "100.00",
    stop: str = "80.00",
    gtt_id: str | None = "551234",
    gtt_trigger: str | None = "80.00",
) -> int:
    store.conn.execute(
        "INSERT INTO tw_position (user_id, broker_account_id, instrument_id, signal_date, "
        "entry_date, entry_avg, entry_adj_factor, quantity_entered, quantity_open, "
        "initial_stop, stop_price, high_since, high_since_date, gtt_id, gtt_trigger, state, "
        "simulated, half_size) VALUES (1, 1, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', "
        "1, 0)",
        (
            instrument_id,
            SESSION.isoformat(),
            SESSION.isoformat(),
            entry,
            quantity,
            quantity,
            stop,
            stop,
            entry,
            SESSION.isoformat(),
            gtt_id,
            gtt_trigger,
        ),
    )
    return int(store.conn.execute("SELECT MAX(id) AS id FROM tw_position").fetchone()["id"])


# =========================================================================================
# The store round-trips against sqlite, with Decimal in and out
# =========================================================================================
class TestTheStore:
    def test_a_plan_and_its_lines_round_trip(self, store) -> None:  # noqa: ANN001
        plan_id, plan_pk = a_plan(store)
        line_id = a_line(store, plan_pk, plan_id)
        plan = store.plan(plan_id)
        line = store.line(line_id)
        assert plan is not None and plan["session_date"] == SESSION
        assert plan["sleeve_equity_inr"] == D("2500000.00")
        assert line is not None and line["symbol"] == "TWTCO"
        assert line["stop_price"] == D("80.00")
        assert line["client_id"] == f"{plan_id}:TWTCO"

    def test_a_malformed_plan_id_is_none_and_not_a_five_hundred(self, store) -> None:  # noqa: ANN001
        assert store.plan("not-a-uuid") is None

    def test_a_line_carries_its_signals_turnover_for_the_confirms_resize(self, store) -> None:  # noqa: ANN001
        """``04`` §10.5 — the 1 %-of-turnover cap is one of "the same caps"."""
        plan_id, plan_pk = a_plan(store)
        line_id = a_line(store, plan_pk, plan_id)
        assert store.line(line_id)["turnover_avg_inr"] == D("500000000")

    def test_the_book_is_only_what_the_sleeve_bought(self, store) -> None:  # noqa: ANN001
        a_position(store)
        book = store.open_positions()
        assert [row["symbol"] for row in book] == ["HELDCO"]
        assert book[0]["stop_price"] == D("80.00")

    def test_the_session_counters_accumulate_and_the_naked_count_is_set(self, store) -> None:  # noqa: ANN001
        store.bump_session(SESSION, mode="DRY_RUN", confirms=1, ratchets=1)
        store.bump_session(SESSION, mode="DRY_RUN", confirms=1, fills=1)
        store.set_naked_count(SESSION, mode="DRY_RUN", naked=2)
        store.set_naked_count(SESSION, mode="DRY_RUN", naked=0)
        row = store.session(SESSION)
        assert row["confirms"] == 2 and row["fills"] == 1 and row["ratchets"] == 1
        assert row["naked_at_1515"] == 0

    def test_entries_taken_counts_this_sessions_buys_from_any_plan(self, store) -> None:  # noqa: ANN001
        store.create_order(
            {
                "instrument_id": 42,
                "signal_date": SESSION,
                "side": "BUY",
                "quantity": 10,
                "stop_price": D("80.00"),
                "state": "CONFIRMED",
                "client_id": "a",
                "simulated": True,
            }
        )
        assert store.entries_taken(SESSION) == 1

    def test_the_sleeves_money_is_its_own(self, store) -> None:  # noqa: ANN001
        a_position(store, quantity=100, entry="100.00")
        money = store.sleeve_money(SESSION)
        assert money.equity_inr == D("2500000.00")
        assert money.open_exposure_inr == D("10000.00")
        assert money.first_live_entries_left == 10

    def test_the_countdown_spends_once_and_never_on_a_simulated_fill(self, store) -> None:  # noqa: ANN001
        """``04`` §6.4 and TW5.3's three refusals, reproduced here (DECISIONS-TW TW6.3)."""
        position_id = a_position(store)
        assert store.count_first_live_entry(
            position_id, session_date=SESSION, now=NOW, live=False
        ) is False
        store.conn.execute("UPDATE tw_position SET simulated = 0 WHERE id = ?", (position_id,))
        assert store.count_first_live_entry(
            position_id, session_date=SESSION, now=NOW, live=True
        ) is True
        assert store.config()["first_live_entries_left"] == 9
        # And not twice for the same fill.
        assert store.count_first_live_entry(
            position_id, session_date=SESSION, now=NOW, live=True
        ) is False
        assert store.config()["first_live_entries_left"] == 9
        audit = store.conn.execute("SELECT * FROM tw_config_audit").fetchall()
        assert [row["key"] for row in audit] == ["first_live_entries_left"]

    def test_zero_capital_audits_the_previous_value(self, store) -> None:  # noqa: ANN001
        before = store.zero_capital(now=NOW, changed_by="twt-halt")
        assert before == D("2500000.00")
        assert store.config()["sleeve_capital_inr"] == D("0")
        row = store.conn.execute(
            "SELECT * FROM tw_config_audit WHERE key = 'sleeve_capital_inr'"
        ).fetchone()
        # sqlite's NUMERIC affinity drops the trailing paise the Postgres column keeps, so the
        # value is compared as a number rather than as a string it does not store.
        assert D(row["old_value"]) == D("2500000.00") and D(row["new_value"]) == D(0)

    def test_expire_plans_moves_only_the_live_ones(self, store) -> None:  # noqa: ANN001
        a_plan(store, expires_in_minutes=25)
        a_plan(store, expires_in_minutes=-60)
        assert store.expire_plans(NOW) == 1


# =========================================================================================
# The page — `05` §2
# =========================================================================================
class TestTheView:
    def test_exits_come_before_entries_in_the_view(self, store) -> None:  # noqa: ANN001
        plan_id, plan_pk = a_plan(store)
        a_line(store, plan_pk, plan_id, kind="BUY_AT_OPEN")
        a_line(
            store,
            plan_pk,
            plan_id,
            kind="RAISE_GTT_STOP",
            instrument_id=43,
            symbol="HELDCO",
            stop="104.00",
            previous="80.00",
        )
        view = W.build_view(store, now=NOW)
        assert [line["kind"] for line in view["lines"]][0] == "RAISE_GTT_STOP"
        assert [line["kind"] for line in view["exits"]] == ["RAISE_GTT_STOP"]
        assert [line["kind"] for line in view["entries"]] == ["BUY_AT_OPEN"]

    def test_an_expired_plan_has_no_confirmable_line(self, store) -> None:  # noqa: ANN001
        """``05`` §2 — the buttons are **gone, not disabled**."""
        plan_id, plan_pk = a_plan(store, expires_in_minutes=-1)
        a_line(store, plan_pk, plan_id)
        view = W.build_view(store, now=NOW)
        assert view["expired"] is True
        assert [line["confirmable"] for line in view["lines"]] == [False]
        assert "expired at" in view["countdown"]

    def test_a_live_plan_says_how_long_it_has_left(self, store) -> None:  # noqa: ANN001
        plan_id, plan_pk = a_plan(store)
        a_line(store, plan_pk, plan_id)
        view = W.build_view(store, now=NOW)
        assert view["expired"] is False
        assert "min" in view["countdown"]
        assert [line["confirmable"] for line in view["lines"]] == [True]

    def test_a_naked_line_is_named_on_the_view(self, store) -> None:  # noqa: ANN001
        a_position(store, gtt_id=None, gtt_trigger=None)
        assert W.build_view(store, now=NOW)["naked"] == ["HELDCO"]

    def test_the_view_carries_the_gate_the_flags_and_the_half_size_counter(self, store) -> None:  # noqa: ANN001
        a_plan(store)
        view = W.build_view(store, now=NOW)
        assert view["breadth"]["gate"] == "OPEN"
        assert view["dry_run"] is True
        assert view["execution_enabled"] is False
        assert view["first_live_entries_left"] == 10
        assert view["max_new_entries_per_session"] == 3

    def test_a_desk_with_no_tw_tables_says_so_instead_of_five_hundreding(self, tmp_path) -> None:  # noqa: ANN001
        conn = sqlite3.connect(tmp_path / "empty.db", isolation_level=None)
        conn.row_factory = sqlite3.Row
        empty = W.PgTwtStore(conn, user_id=1, schema="")
        with pytest.raises(sqlite3.OperationalError):
            W.build_view(empty, now=NOW)
        view = W.unavailable_view("no such table: tw_plan", now=NOW)
        assert view["available"] is False and view["lines"] == []


# =========================================================================================
# The routes — TW6a's six, plus the page
# =========================================================================================
@pytest.fixture
def client(store, monkeypatch, tmp_path):  # noqa: ANN001, ANN201
    """The mounted app with the store pointed at the scenario's sqlite file and no Kite."""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(C, "TWT_EXECUTION_ENABLED", False)
    monkeypatch.setattr(C, "DRY_RUN", True)
    monkeypatch.setattr(M, "_kite", None)
    monkeypatch.setattr(W, "open_store", lambda: contextlib.nullcontext(store))
    monkeypatch.setattr(W, "_now", lambda: NOW)
    monkeypatch.setattr(W, "twt_gateway", lambda: _Gateway())
    monkeypatch.setattr(W, "last_price", lambda symbol: LAST_PRICES.get(symbol))
    monkeypatch.setattr(W, "gtt_source", lambda: None)
    monkeypatch.setattr(C, "TOKEN_FILE", str(tmp_path / "no-such-token.json"))
    return TestClient(M.app, headers={"Origin": "http://testserver:8420"})


#: What the broker would quote. Every executable kind needs one: the entry is a MARKET order and
#: both GTT kinds are refused without a price to check the trigger against.
LAST_PRICES = {"TWTCO": D("100.00"), "HELDCO": D("130.00")}


class _Gateway:
    """A gateway that simulates, and a broker that is never reached.

    The route tests are about the routes; ``tests/test_twt_execute.py`` drives the same paths
    through the **real** gateway over a broker client that explodes.
    """

    async def place(self, **_: object) -> dict:
        return {"status": "DRY_RUN", "order_id": "DRY-1"}

    async def place_gtt_stop(self, **kwargs: object) -> dict:
        return {
            "status": "DRY_RUN_GTT",
            "gtt_id": None,
            "client_id": kwargs.get("client_id"),
            "trigger": kwargs.get("trigger"),
        }

    async def delete_gtt(self, **kwargs: object) -> dict:
        return {"status": "DRY_RUN_GTT_DELETE", "gtt_id": kwargs.get("gtt_id")}


def _forms(html: str, action: str) -> list[str]:
    return re.findall(r'<form[^>]*action="' + re.escape(action) + r'"[^>]*>.*?</form>', html, re.S)


class TestThePageRenders:
    def test_the_page_renders_with_exits_entries_and_the_book(self, client, store) -> None:  # noqa: ANN001
        plan_id, plan_pk = a_plan(store)
        a_line(store, plan_pk, plan_id)
        a_position(store)
        html = client.get("/twt").text
        assert "Three weeks tight" in html
        assert "Exits" in html and "Entries" in html and "The book" in html
        assert "SIMULATED" in html

    def test_there_is_one_confirm_form_per_line_and_no_confirm_all(self, client, store) -> None:  # noqa: ANN001
        plan_id, plan_pk = a_plan(store)
        a_line(store, plan_pk, plan_id)
        a_line(store, plan_pk, plan_id, kind="ARM_GTT", instrument_id=43, symbol="HELDCO")
        html = client.get("/twt").text
        forms = _forms(html, "/twt/execute")
        assert len(forms) == 2
        assert all('name="line_id"' in form for form in forms)
        assert "confirm all" not in html.lower()

    def test_an_expired_plans_buttons_are_absent_not_disabled(self, client, store) -> None:  # noqa: ANN001
        plan_id, plan_pk = a_plan(store, expires_in_minutes=-1)
        a_line(store, plan_pk, plan_id)
        html = client.get("/twt").text
        assert _forms(html, "/twt/execute") == []
        assert "disabled" not in html
        assert "plan expired" in html

    def test_the_rearm_form_exists_only_for_a_naked_line(self, client, store) -> None:  # noqa: ANN001
        a_position(store, instrument_id=42, gtt_id="551234")
        a_position(store, instrument_id=43, gtt_id=None, gtt_trigger=None)
        html = client.get("/twt").text
        forms = _forms(html, "/twt/rearm")
        assert len(forms) == 1
        assert "NAKED" in html

    def test_the_data_route_answers_json(self, client, store) -> None:  # noqa: ANN001
        plan_id, plan_pk = a_plan(store)
        a_line(store, plan_pk, plan_id)
        payload = client.get("/twt/data").json()
        assert payload["available"] is True
        assert payload["plan"]["plan_id"] == plan_id
        assert D(payload["lines"][0]["value_inr"]) == D("250000.00")


class TestTheExecuteRoute:
    def test_a_confirm_fills_and_arms_through_the_route(self, client, store) -> None:  # noqa: ANN001
        plan_id, plan_pk = a_plan(store)
        line_id = a_line(store, plan_pk, plan_id)
        answer = client.post(
            "/twt/execute",
            data={"plan_id": plan_id, "line_id": line_id, "confirm": "true"},
        )
        assert answer.status_code == 200, answer.text
        body = answer.json()
        assert body["status"] == "SIMULATED"
        assert body["line_id"] == line_id
        position = store.position(int(body["position_id"]))
        assert position["gtt_id"] is not None
        assert position["initial_stop"] == D("80.00")

    def test_a_missing_confirm_is_a_four_hundred_through_the_route(self, client, store) -> None:  # noqa: ANN001
        plan_id, plan_pk = a_plan(store)
        line_id = a_line(store, plan_pk, plan_id)
        answer = client.post(
            "/twt/execute", data={"plan_id": plan_id, "line_id": line_id, "confirm": "false"}
        )
        assert answer.status_code == 400

    def test_an_expired_plan_is_a_four_ten_through_the_route(self, client, store) -> None:  # noqa: ANN001
        plan_id, plan_pk = a_plan(store, expires_in_minutes=-1)
        line_id = a_line(store, plan_pk, plan_id)
        answer = client.post(
            "/twt/execute", data={"plan_id": plan_id, "line_id": line_id, "confirm": "true"}
        )
        assert answer.status_code == 410


class TestTheUntouchableGuard:
    """Non-negotiable 7, as an **answer** rather than as a traceback.

    ``guards.assert_tradeable`` raises on an SGB, a G-sec or an ``EXCLUDED_SYMBOLS`` name at the
    lowest layer, before any network call — which is right, and is the wrong shape for a route: a
    500 tells a person nothing at 09:15. This sleeve's universe holds no such instrument and it
    should never see one, which is exactly why the answer is worth having.
    """

    def test_an_untouchable_instrument_comes_back_blocked_with_the_guards_words(
        self, client, store, monkeypatch
    ) -> None:  # noqa: ANN001
        from app.core.guards import UntouchableInstrumentError

        class _Guarded(_Gateway):
            async def place(self, **_: object) -> dict:
                raise UntouchableInstrumentError("SGBMAY29: sovereign gold bonds are untouchable")

        monkeypatch.setattr(W, "twt_gateway", lambda: _Guarded())
        plan_id, plan_pk = a_plan(store)
        line_id = a_line(store, plan_pk, plan_id)

        answer = client.post(
            "/twt/execute", data={"plan_id": plan_id, "line_id": line_id, "confirm": "true"}
        )

        assert answer.status_code == 200, answer.text
        body = answer.json()
        assert body["status"] == "BLOCKED"
        assert "untouchable" in body["reason"]
        assert body["simulated"] is True

    def test_a_rearm_of_an_untouchable_line_is_blocked_not_a_five_hundred(
        self, client, store, monkeypatch
    ) -> None:  # noqa: ANN001
        from app.core.guards import UntouchableInstrumentError

        class _Guarded(_Gateway):
            async def place_gtt_stop(self, **_: object) -> dict:
                raise UntouchableInstrumentError("SGBMAY29: sovereign gold bonds are untouchable")

        monkeypatch.setattr(W, "twt_gateway", lambda: _Guarded())
        position_id = a_position(store, gtt_id=None, gtt_trigger=None)

        body = client.post(
            "/twt/rearm", data={"position_id": position_id, "confirm": "true"}
        ).json()

        assert body["status"] == "BLOCKED"
        assert "untouchable" in body["reason"]


class TestTheHaltRoute:
    """FIRST-LIVE-MORNING §7, to the spelling the runbook uses."""

    def test_halt_zeroes_the_capital_expires_the_plans_and_answers_in_one_line(
        self, client, store
    ) -> None:  # noqa: ANN001
        a_plan(store)
        answer = client.post("/twt/halt", data={"confirm": "true"})
        assert answer.status_code == 200, answer.text
        body = answer.json()
        assert body["halted"] is True
        assert D(body["sleeve_capital_inr_before"]) == D("2500000.00")
        assert body["plans_expired"] == 1
        assert body["message"].count("\n") == 0
        assert store.config()["sleeve_capital_inr"] == D("0")

    def test_halt_never_touches_protection(self, client, store) -> None:  # noqa: ANN001
        """**The behaviour a test must pin.** The resting GTT is still resting afterwards, and
        a ``RAISE_GTT_STOP`` still plans and still arms — through the route, on a halted
        sleeve."""
        position_id = a_position(store, instrument_id=43, gtt_id="551234", gtt_trigger="80.00")
        client.post("/twt/halt", data={"confirm": "true"})

        after = store.position(position_id)
        assert after["gtt_id"] == "551234"
        assert after["gtt_trigger"] == D("80.00")
        assert after["quantity_open"] == 1_000 and after["state"] == "OPEN"

        plan_id, plan_pk = a_plan(store)
        line_id = a_line(
            store,
            plan_pk,
            plan_id,
            kind="RAISE_GTT_STOP",
            instrument_id=43,
            symbol="HELDCO",
            stop="104.00",
            previous="80.00",
        )
        answer = client.post(
            "/twt/execute", data={"plan_id": plan_id, "line_id": line_id, "confirm": "true"}
        )
        assert answer.json()["status"] == "SIMULATED", answer.text
        assert store.position(position_id)["gtt_trigger"] == D("104.00")

    def test_a_halted_sleeve_cannot_buy(self, client, store) -> None:  # noqa: ANN001
        client.post("/twt/halt", data={"confirm": "true"})
        plan_id, plan_pk = a_plan(store)
        line_id = a_line(store, plan_pk, plan_id)
        body = client.post(
            "/twt/execute", data={"plan_id": plan_id, "line_id": line_id, "confirm": "true"}
        ).json()
        assert body["status"] == "BLOCKED"
        assert body["reason"].startswith("NO_SLEEVE_CAPITAL")

    def test_halt_without_a_confirm_is_a_four_hundred(self, client, store) -> None:  # noqa: ANN001
        assert client.post("/twt/halt", data={"confirm": "no"}).status_code == 400
        assert store.config()["sleeve_capital_inr"] == D("2500000.00")


class TestTheRearmSweepAndReconcileRoutes:
    def test_rearm_arms_one_naked_position(self, client, store) -> None:  # noqa: ANN001
        position_id = a_position(store, gtt_id=None, gtt_trigger=None)
        body = client.post(
            "/twt/rearm", data={"position_id": position_id, "confirm": "true"}
        ).json()
        assert body["status"] == "SIMULATED"
        assert store.position(position_id)["gtt_id"] is not None

    def test_rearm_without_a_confirm_is_a_four_hundred(self, client, store) -> None:  # noqa: ANN001
        position_id = a_position(store, gtt_id=None, gtt_trigger=None)
        answer = client.post(
            "/twt/rearm", data={"position_id": position_id, "confirm": "no"}
        )
        assert answer.status_code == 400

    def test_the_sweep_route_reports_zero_naked_when_it_is_clean(self, client, store) -> None:  # noqa: ANN001
        a_position(store, gtt_id=None, gtt_trigger=None)
        body = client.post("/twt/sweep", data={"confirm": "true"}).json()
        assert body["naked_before"] == 1
        assert body["naked"] == 0
        assert store.session(NOW.date())["naked_at_1515"] == 0

    def test_the_sweep_route_needs_a_confirm(self, client) -> None:  # noqa: ANN001
        assert client.post("/twt/sweep", data={"confirm": "no"}).status_code == 400

    def test_the_reconcile_route_attaches_a_hand_armed_gtt(
        self, client, store, monkeypatch
    ) -> None:  # noqa: ANN001
        position_id = a_position(store, gtt_id=None, gtt_trigger=None)
        monkeypatch.setattr(
            W,
            "gtt_source",
            lambda: _Gtts([{"gtt_id": 771122, "symbol": "HELDCO", "trigger": 81.5}]),
        )
        body = client.post("/twt/reconcile", data={"confirm": "true"}).json()
        assert body["attached"] == 1
        assert store.position(position_id)["gtt_id"] == "771122"

    def test_the_reconcile_route_needs_a_confirm(self, client) -> None:  # noqa: ANN001
        assert client.post("/twt/reconcile", data={"confirm": "no"}).status_code == 400


class _Gtts:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def list_gtts(self) -> list[dict]:
        return self.rows


# =========================================================================================
# G9 — every POST to the desk must name its origin
# =========================================================================================
class TestEveryPostNeedsAnOrigin:
    """FIRST-LIVE-MORNING §1's second detail, the one that would cost a morning.

    ``DeskSecurity`` refuses a state-changing request that cannot say where it came from. A
    browser sends the header; ``curl`` does not unless told, and the 403 reads exactly like a
    permissions problem. Every phone command in the runbook carries ``-H "Origin: $DESK"``
    because of this, and these tests are why the runbook says so.
    """

    @pytest.fixture
    def no_origin(self, store, monkeypatch, tmp_path):  # noqa: ANN001, ANN201
        """A client that sends **no** ``Origin`` — which is what ``curl`` does.

        ``tests/conftest.py`` adds the header to every ``TestClient`` by default, because a
        browser sends it on every POST and route tests are about routes. This fixture takes it
        back off, because these tests are about the header itself.
        """
        from fastapi.testclient import TestClient

        monkeypatch.setattr(M, "_kite", None)
        monkeypatch.setattr(W, "open_store", lambda: contextlib.nullcontext(store))
        monkeypatch.setattr(W, "_now", lambda: NOW)
        monkeypatch.setattr(W, "twt_gateway", lambda: _Gateway())
        monkeypatch.setattr(C, "TOKEN_FILE", str(tmp_path / "no-such-token.json"))
        client = TestClient(M.app)
        client.headers.pop("origin", None)
        return client

    @pytest.mark.parametrize(
        "path,payload",
        [
            ("/twt/execute", {"plan_id": str(uuid.uuid4()), "line_id": 1, "confirm": "true"}),
            ("/twt/halt", {"confirm": "true"}),
            ("/twt/rearm", {"position_id": 1, "confirm": "true"}),
            ("/twt/sweep", {"confirm": "true"}),
            ("/twt/reconcile", {"confirm": "true"}),
        ],
    )
    def test_a_post_without_an_origin_header_is_refused(
        self, no_origin, store, path, payload
    ) -> None:  # noqa: ANN001
        answer = no_origin.post(path, data=payload)
        assert answer.status_code == 403
        assert "did not come from the desk" in answer.json()["detail"]
        # And nothing happened: the halt in particular did not zero anything.
        assert store.config()["sleeve_capital_inr"] == D("2500000.00")

    def test_a_post_from_another_sites_origin_is_refused(self, store, monkeypatch) -> None:  # noqa: ANN001
        from fastapi.testclient import TestClient

        monkeypatch.setattr(W, "open_store", lambda: contextlib.nullcontext(store))
        evil = TestClient(M.app, headers={"Origin": "http://evil.example"})
        assert evil.post("/twt/halt", data={"confirm": "true"}).status_code == 403
        assert store.config()["sleeve_capital_inr"] == D("2500000.00")

    def test_the_page_itself_is_a_get_and_needs_no_origin(self, no_origin) -> None:  # noqa: ANN001
        assert no_origin.get("/twt").status_code == 200
        assert no_origin.get("/twt/data").status_code == 200


# =========================================================================================
# Law 2, restated for a page: this module names no broker and no placing verb
# =========================================================================================
class TestTheDeskModuleNamesNoBroker:
    def test_the_desk_module_never_calls_a_placing_verb_itself(self) -> None:
        """Every order goes through ``app.twt_execute``, which goes through the gateway.

        ``KiteGtts.list_gtts`` is the one broker call in this file and it is a **read**: there is
        no method here that could cancel a trigger, which is deliberate — if Baskfy and Kite
        disagree about whether a GTT exists, the safe direction is always *more* protection.
        """
        source = (W.__file__,)
        text = open(source[0], encoding="utf-8").read()  # noqa: PTH123, SIM115
        for verb in ("kc.place_order", "kc.place_gtt", "kc.delete_gtt", "kc.cancel_order"):
            assert verb not in text, f"{verb} is named in app/twt_desk.py"
        assert "get_gtts" in text  # the one read, named on purpose
