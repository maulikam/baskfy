"""The volume-breakout desk page, its store and its route (VB6, ``docs/vbt/05`` §3).

What is asserted, and where it comes from:

* `05` §3: three panels and a status bar; every line prefixed **VBT**; exits first, then
  cancels, then the GTT backstop, then the buys; the skips with their reasons under the plan;
  a "cancels tonight" mark on a limit in its third session; a naked position in red; **there is
  no "confirm all"**, and the page's only form posts exactly one line.
* `POST /vbt/execute` goes through ``app.vbt_execute.execute_line`` and returns its outcome as
  JSON; 400 without a confirm and 410 past the expiry, through the route.
* The ``VbtStore`` protocol: every method round-trips against sqlite with ``Decimal`` in and out.
* Law 2, restated for a page: the module never names a placing verb or the broker.

THE DATABASE
------------
The ``vb_`` tables are built here from a DDL that mirrors ``alembic/versions/0037_vbt.py`` for
the columns the store reads, in sqlite's spelling, on a per-test file — and the store runs the
same SQL it runs on Postgres with the schema prefix set to ``""``. A desk suite cannot depend on
a Postgres being up, and a fake connection would prove nothing about the SQL.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import pathlib
import sqlite3
import uuid
from decimal import Decimal

import pytest

from app import vbt_desk as W

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
NOW = dt.datetime(2026, 9, 9, 21, 20, tzinfo=IST)
SESSION = dt.date(2026, 9, 9)
D = Decimal

DDL = """
CREATE TABLE instrument (id INTEGER PRIMARY KEY, symbol TEXT);
CREATE TABLE vb_config (
  user_id INTEGER PRIMARY KEY, sleeve_capital_inr NUMERIC, max_open_positions INTEGER,
  max_position_pct NUMERIC, stop_pct NUMERIC, dry_run_sessions INTEGER,
  first_live_sessions_left INTEGER);
CREATE TABLE vb_breadth_daily (
  user_id INTEGER, date TEXT, measured_count INTEGER, above_count INTEGER,
  pct_above_dma NUMERIC, gate TEXT, thin_session INTEGER, PRIMARY KEY (user_id, date));
CREATE TABLE vb_plan (
  id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT, user_id INTEGER, session_date TEXT,
  source TEXT, built_at TEXT, expires_at TEXT, plan_hash TEXT, gate TEXT,
  sleeve_equity_inr NUMERIC, total_new_exposure_inr NUMERIC);
CREATE TABLE vb_plan_line (
  id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER, user_id INTEGER, instrument_id INTEGER,
  kind TEXT, state TEXT, quantity INTEGER, limit_price NUMERIC, stop_price NUMERIC,
  value_inr NUMERIC, size_cap TEXT, reason TEXT, note TEXT, client_id TEXT, journal_ref TEXT,
  order_id INTEGER, position_id INTEGER);
CREATE TABLE vb_plan_skip (
  id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER, user_id INTEGER, instrument_id INTEGER,
  reason TEXT, detail TEXT);
CREATE TABLE vb_position (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, broker_account_id INTEGER,
  instrument_id INTEGER, entry_date TEXT, entry_avg NUMERIC, quantity_entered INTEGER,
  quantity_open INTEGER, initial_stop NUMERIC, stop_price NUMERIC, gtt_id TEXT,
  gtt_trigger NUMERIC, gtt_armed_at TEXT, state TEXT, exit_queued_for TEXT,
  exit_reason_queued TEXT, closed_on TEXT, exit_avg NUMERIC, close_reason TEXT,
  pnl_inr NUMERIC, return_pct NUMERIC, simulated INTEGER);
CREATE TABLE vb_order (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, broker_account_id INTEGER,
  instrument_id INTEGER, signal_date TEXT, limit_price NUMERIC, stop_price NUMERIC,
  quantity INTEGER, state TEXT, working_from TEXT, expires_after_session TEXT,
  sessions_worked INTEGER DEFAULT 0, broker_order_id TEXT, client_id TEXT,
  filled_quantity INTEGER DEFAULT 0, avg_fill_price NUMERIC, position_id INTEGER,
  cancelled_on TEXT, cancel_reason TEXT, simulated INTEGER);
CREATE TABLE vb_fill (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, position_id INTEGER, order_id INTEGER,
  side TEXT, quantity INTEGER, price NUMERIC, filled_at TEXT, journal_ref TEXT,
  simulated INTEGER);
CREATE TABLE vb_scan_run (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, requested_at TEXT, started_at TEXT,
  finished_at TEXT, session_date TEXT, status TEXT DEFAULT 'QUEUED', source TEXT DEFAULT 'desk',
  detail TEXT, error TEXT, task_id TEXT);
CREATE TABLE vb_session (
  user_id INTEGER, session_date TEXT, mode TEXT DEFAULT 'DRY_RUN', gate TEXT DEFAULT 'SHUT',
  signals INTEGER DEFAULT 0, confirms INTEGER DEFAULT 0, fills INTEGER DEFAULT 0,
  exits INTEGER DEFAULT 0, PRIMARY KEY (user_id, session_date));
"""


@pytest.fixture
def store(tmp_path):  # noqa: ANN001, ANN201 - the desk's own fixture idiom
    conn = sqlite3.connect(tmp_path / "vbt.db", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    conn.execute("INSERT INTO instrument (id, symbol) VALUES (42, 'VBTCO'), (43, 'OTHERCO')")
    conn.execute(
        "INSERT INTO vb_config VALUES (1, '1000000.00', 10, '12.50', '12.00', 7, 5)"
    )
    conn.execute(
        "INSERT INTO vb_breadth_daily VALUES (1, ?, 1200, 700, '58.3333', 'OPEN', 0)",
        (SESSION.isoformat(),),
    )
    return W.PgVbtStore(conn, user_id=1, schema="", broker_account_id=1)


def a_plan(store, *, expires_in_minutes: int = 25) -> tuple[str, int]:  # noqa: ANN001
    plan_id = str(uuid.uuid4())
    store.conn.execute(
        "INSERT INTO vb_plan (plan_id, user_id, session_date, source, built_at, expires_at, "
        "plan_hash, gate, sleeve_equity_inr, total_new_exposure_inr) "
        "VALUES (?, 1, ?, 'EVENING', ?, ?, 'abc', 'OPEN', '1000000.00', '96000.00')",
        (
            plan_id,
            SESSION.isoformat(),
            (NOW - dt.timedelta(minutes=5)).isoformat(),
            (NOW + dt.timedelta(minutes=expires_in_minutes)).isoformat(),
        ),
    )
    plan_pk = store.conn.execute("SELECT id FROM vb_plan").fetchone()["id"]
    return plan_id, int(plan_pk)


def a_line(store, plan_pk: int, kind: str = "PLACE_LIMIT", **overrides) -> int:  # noqa: ANN001
    fields = {
        "plan_id": plan_pk,
        "user_id": 1,
        "instrument_id": 42,
        "kind": kind,
        "state": "PROPOSED",
        "quantity": 1_000,
        "limit_price": "96.00",
        "stop_price": "84.45",
        "value_inr": "96000.00",
        "size_cap": "SLOT",
        "note": "limit at the signal close; slot bound the size",
        "client_id": f"plan:VBTCO:{kind}",
        **overrides,
    }
    columns = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    store.conn.execute(
        f"INSERT INTO vb_plan_line ({columns}) VALUES ({marks})", tuple(fields.values())
    )
    return int(store.conn.execute("SELECT MAX(id) AS id FROM vb_plan_line").fetchone()["id"])


class TestTheStoreRoundTrips:
    def test_a_plan_comes_back_with_decimals_and_aware_stamps(self, store) -> None:  # noqa: ANN001
        plan_id, _ = a_plan(store)
        row = store.plan(plan_id)
        assert row is not None
        assert row["sleeve_equity_inr"] == D("1000000.00")
        assert row["session_date"] == SESSION
        assert row["expires_at"].tzinfo is not None

    def test_a_malformed_plan_id_is_none_rather_than_a_driver_error(self, store) -> None:  # noqa: ANN001
        """`vb_plan.plan_id` is a uuid column on Postgres; a form post that is not one must be
        a 404, not a 500."""
        assert store.plan("not-a-uuid") is None

    def test_a_line_carries_its_plan_and_its_levels(self, store) -> None:  # noqa: ANN001
        plan_id, plan_pk = a_plan(store)
        line_id = a_line(store, plan_pk)
        line = store.line(line_id)
        assert line is not None
        assert line["plan_id"] == plan_id
        assert line["limit_price"] == D("96.00")
        assert line["stop_price"] == D("84.45")
        assert line["symbol"] == "VBTCO"

    def test_setting_a_line_writes_only_what_it_was_given(self, store) -> None:  # noqa: ANN001
        _plan_id, plan_pk = a_plan(store)
        line_id = a_line(store, plan_pk)
        store.set_line(line_id, state="SENT", journal_ref="ORD-1")
        line = store.line(line_id)
        assert line["state"] == "SENT"
        assert line["journal_ref"] == "ORD-1"
        assert line["note"].startswith("limit at the signal close")

    def test_a_position_round_trips(self, store) -> None:  # noqa: ANN001
        position_id = store.create_position(
            {
                "instrument_id": 42,
                "entry_date": SESSION.isoformat(),
                "entry_avg": "96.00",
                "quantity_entered": 500,
                "quantity_open": 500,
                "initial_stop": "84.45",
                "stop_price": "84.45",
                "state": "OPEN",
                "simulated": 1,
            }
        )
        row = store.position(position_id)
        assert row["entry_avg"] == D("96.00")
        assert row["simulated"] is True
        assert store.open_position_for(42)["id"] == position_id

    def test_an_order_round_trips_and_is_found_while_it_works(self, store) -> None:  # noqa: ANN001
        order_id = store.create_order(
            {
                "instrument_id": 42,
                "signal_date": SESSION.isoformat(),
                "limit_price": "96.00",
                "stop_price": "84.45",
                "quantity": 1_000,
                "state": "SENT",
                "simulated": 1,
            }
        )
        assert store.working_order_for(42)["id"] == order_id
        store.update_order(order_id, {"state": "CANCELLED"})
        assert store.working_order_for(42) is None

    def test_the_session_counters_accumulate(self, store) -> None:  # noqa: ANN001
        store.bump_session(SESSION, mode="DRY_RUN", confirms=1)
        store.bump_session(SESSION, mode="DRY_RUN", confirms=1, fills=1)
        row = store.session(SESSION)
        assert (row["confirms"], row["fills"], row["mode"]) == (2, 1, "DRY_RUN")

    def test_entries_taken_counts_only_what_the_session_committed(self, store) -> None:  # noqa: ANN001
        store.create_order(
            {
                "instrument_id": 42,
                "signal_date": SESSION.isoformat(),
                "limit_price": "96.00",
                "stop_price": "84.45",
                "quantity": 10,
                "state": "SENT",
            }
        )
        store.create_order(
            {
                "instrument_id": 43,
                "signal_date": SESSION.isoformat(),
                "limit_price": "50.00",
                "stop_price": "44.00",
                "quantity": 10,
                "state": "EXPIRED",
            }
        )
        assert store.entries_taken(SESSION) == 1


class TestTheView:
    def test_the_lines_are_ordered_risk_off_before_risk_on(self, store) -> None:  # noqa: ANN001
        """`04` §9.3 — a person reading down the page reads the risk coming off first."""
        _plan_id, plan_pk = a_plan(store)
        a_line(store, plan_pk, "PLACE_LIMIT")
        a_line(store, plan_pk, "ARM_GTT")
        a_line(store, plan_pk, "SELL_AT_OPEN")
        a_line(store, plan_pk, "CANCEL_LIMIT")
        view = W.build_view(store, now=NOW)
        assert [line["kind"] for line in view["lines"]] == [
            "SELL_AT_OPEN",
            "CANCEL_LIMIT",
            "ARM_GTT",
            "PLACE_LIMIT",
        ]

    def test_every_line_is_labelled_vbt(self, store) -> None:  # noqa: ANN001
        """`05` §3 — so nobody confuses it with the weekly book's or the swing book's."""
        _plan_id, plan_pk = a_plan(store)
        a_line(store, plan_pk)
        view = W.build_view(store, now=NOW)
        assert view["lines"][0]["label"].startswith("VBT")

    def test_a_proposed_line_on_a_live_plan_is_confirmable(self, store) -> None:  # noqa: ANN001
        _plan_id, plan_pk = a_plan(store)
        a_line(store, plan_pk)
        assert W.build_view(store, now=NOW)["lines"][0]["confirmable"] is True

    def test_a_line_on_an_expired_plan_offers_no_button(self, store) -> None:  # noqa: ANN001
        """A page left open for an hour must not offer a button that can only 410."""
        _plan_id, plan_pk = a_plan(store, expires_in_minutes=-1)
        a_line(store, plan_pk)
        line = W.build_view(store, now=NOW)["lines"][0]
        assert line["confirmable"] is False
        assert line["expired"] is True

    def test_the_skips_are_on_the_page(self, store) -> None:  # noqa: ANN001
        """A plan is not honest without them."""
        _plan_id, plan_pk = a_plan(store)
        store.conn.execute(
            "INSERT INTO vb_plan_skip (plan_id, user_id, instrument_id, reason, detail) "
            "VALUES (?, 1, 43, 'SESSION_CAP', '3 new entries a session')",
            (plan_pk,),
        )
        view = W.build_view(store, now=NOW)
        assert view["skips"] == [
            {"reason": "SESSION_CAP", "detail": "3 new entries a session", "symbol": "OTHERCO"}
        ]

    def test_a_limit_in_its_third_session_says_it_cancels_tonight(self, store) -> None:  # noqa: ANN001
        store.create_order(
            {
                "instrument_id": 42,
                "signal_date": SESSION.isoformat(),
                "limit_price": "96.00",
                "stop_price": "84.45",
                "quantity": 100,
                "state": "SENT",
                "sessions_worked": 2,
            }
        )
        order = W.build_view(store, now=NOW)["working"][0]
        assert order["sessions_text"] == "2 of 3"
        assert order["expires_tonight"] is True

    def test_a_position_without_a_gtt_is_marked_naked(self, store) -> None:  # noqa: ANN001
        """The one state the method forbids."""
        store.create_position(
            {
                "instrument_id": 42,
                "entry_date": SESSION.isoformat(),
                "entry_avg": "96.00",
                "quantity_entered": 100,
                "quantity_open": 100,
                "initial_stop": "84.45",
                "stop_price": "84.45",
                "state": "OPEN",
                "simulated": 1,
            }
        )
        assert W.build_view(store, now=NOW)["book"][0]["naked"] is True

    def test_the_status_bar_carries_the_gate_and_the_dry_run_count(self, store) -> None:  # noqa: ANN001
        view = W.build_view(store, now=NOW)
        assert view["breadth"]["gate"] == "OPEN"
        assert view["dry_run_sessions"] == 7
        # 0 since 11 Sep 2026: the paper gate was withdrawn (DECISIONS-VB VB11.4).
        assert view["dry_run_sessions_required"] == 0
        assert view["execution_enabled"] is False
        assert view["dry_run"] is True

    def test_a_desk_with_no_tables_says_so_rather_than_five_hundred(self) -> None:
        view = W.unavailable_view("no vb_ tables on this desk", now=NOW)
        assert view["available"] is False
        assert "no vb_ tables" in view["reason"]
        assert view["lines"] == []


class TestTheLaw:
    def test_the_page_module_names_no_broker_method(self) -> None:
        source = pathlib.Path(W.__file__).read_text(encoding="utf-8")
        for forbidden in ("place_order", "place_gtt(", "kc.", "kite.kc"):
            assert forbidden not in source, forbidden

    def test_there_is_exactly_one_execute_route_and_it_takes_one_line(self) -> None:
        """`05` §3: "There is no 'confirm all'." A route that took a list would be one.

        Two POSTs since VB12, and the distinction is the point rather than the count:
        `/vbt/execute` is the one doorway to the gateway, and `/vbt/rescan` writes a single
        `vb_scan_run` row and hands off to a worker with no order path. The assertion below is
        that **exactly one route can reach an order**, which is what Track C §3 is about; a third
        POST would have to justify itself here.
        """
        posts = [
            route
            for route in W.router.routes
            if "POST" in getattr(route, "methods", set())
        ]
        assert sorted(route.path for route in posts) == ["/vbt/execute", "/vbt/rescan"]
        posts = [route for route in posts if route.path == "/vbt/execute"]
        # `from __future__ import annotations` in the module leaves these as strings, which is
        # what the assertion has to read — the point is the shape of the signature, not its
        # evaluation: one plan, one line, one confirm, and no list anywhere in it.
        parameters = posts[0].endpoint.__annotations__
        assert set(parameters) >= {"plan_id", "line_id", "confirm"}
        assert parameters["line_id"] == "int"
        assert not any("list" in str(kind) for kind in parameters.values())

    def test_the_rescan_route_cannot_reach_an_order(self) -> None:
        """VB12: the second POST writes a row and names nothing that could place.

        Asserted over the handler's own source, with its docstring stripped, because the
        docstring explains at length what it is *not* allowed to do.
        """
        import inspect
        import re

        route = next(
            r for r in W.router.routes if getattr(r, "path", "") == "/vbt/rescan"
        )
        source = inspect.getsource(route.endpoint)
        code = re.sub(r'("""|\'\'\')(?:.|\n)*?\1', " ", source)
        for forbidden in ("execute_line", "gateway", "place", "kc.", "gtt", "confirm"):
            assert forbidden not in code.lower(), f"the rescan route names {forbidden}"
        assert "request_scan" in code, "the rescan route does not write a scan row at all"

    def test_the_template_has_no_confirm_all_control(self) -> None:
        template = pathlib.Path(W.__file__).parent / "templates" / "vbt.html"
        body = template.read_text(encoding="utf-8")
        assert body.count('action="/vbt/execute"') == 1
        assert "confirm_all" not in body
        assert "confirm-all" not in body

    def test_no_auto_execute_flag_is_referenced(self) -> None:
        source = pathlib.Path(W.__file__).read_text(encoding="utf-8").upper()
        assert "AUTO_EXECUTE" not in source
