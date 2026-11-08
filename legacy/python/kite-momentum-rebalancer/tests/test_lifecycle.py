"""LV6 — one lifecycle per trade, the stop judged at the broker, and explicit adoption.

`gates/live-6-lifecycle.md`. The books are the executor suites' in-memory stores given a sqlite
``conn`` for the two reads adoption needs (an instrument by symbol, the ``lv_adoption`` row); the
broker's GTT list is a dict; the gateway is a recorder that answers ``GTT_PLACED``.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import inspect
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import lifecycle as L
from app import main as M
from app import reconcile as R
from app.exit_rules import EXIT_RULES
from tests import test_swing_execute as swing
from tests import test_twt_execute as twt
from tests import test_vbt_execute as vbt

IST = L.IST
NOW = dt.datetime(2026, 9, 28, 11, 0, tzinfo=IST)
D = Decimal
TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "templates"


def run(coro):  # noqa: ANN001, ANN201 - the desk's own test idiom
    return asyncio.run(coro)


def gtt(gtt_id: str, symbol: str, quantity: int, *, status: str = "active", trigger: str = "80") -> dict:
    return {
        "id": gtt_id,
        "status": status,
        "condition": {"tradingsymbol": symbol, "trigger_values": [float(trigger)]},
        "orders": [{"transaction_type": "SELL", "quantity": quantity}],
    }


def position(sleeve: str, pid: int, symbol: str, *, qty: int = 100, entry: str = "100", stop: str = "80", gtt_id: str | None = "1") -> dict:
    row = {
        "id": pid,
        "symbol": symbol,
        "entry_date": dt.date(2026, 9, 25),
        "entry_avg": D(entry),
        "quantity_entered": qty,
        "quantity_open": qty,
        "initial_stop": D(stop),
        "gtt_id": gtt_id,
        "simulated": False,
    }
    row["stop" if sleeve == "swing" else "stop_price"] = D(stop)
    return row


class TestRowsAndPage:
    def test_rows_cover_every_sleeve_with_entry_quantities_risk_and_the_exact_exit_rule(self) -> None:
        positions = {
            "swing": [position("swing", 1, "SWINGCO", qty=300, entry="120", stop="110", gtt_id="11")],
            "twt": [position("twt", 2, "TWTCO", qty=100, entry="100", stop="80", gtt_id="22")],
            "vbt": [position("vbt", 3, "VBTCO", qty=50, entry="96", stop="84.45", gtt_id="33")],
        }
        gtts = R.gtt_index([gtt("11", "SWINGCO", 300), gtt("22", "TWTCO", 100), gtt("33", "VBTCO", 50)])
        rows = L.build_rows(positions, gtts=gtts, issues=[], now=NOW)
        by = {r.symbol: r for r in rows}
        assert set(by) == {"SWINGCO", "TWTCO", "VBTCO"}
        assert by["TWTCO"].rupee_risk == D("2000.00") and by["TWTCO"].filled_qty == 100
        assert by["SWINGCO"].rupee_risk == D("3000.00")
        assert by["VBTCO"].rupee_risk == D("577.50")
        assert all(r.stop_state == L.ARMED and r.resolved for r in rows)
        assert by["TWTCO"].exit_rule == EXIT_RULES["twt"] and "20%" in by["TWTCO"].exit_rule
        assert "20-day MA" in by["VBTCO"].exit_rule and "trails" in by["SWINGCO"].exit_rule

    def test_rows_put_unresolved_and_overdue_first_and_carry_the_reconcilers_findings(self) -> None:
        positions = {"twt": [position("twt", 1, "GOOD", gtt_id="1"), position("twt", 2, "NAKEDCO", gtt_id=None)]}
        issues = [{"sleeve": "twt", "position_id": 2, "symbol": "NAKEDCO", "kind": "NAKED", "detail": "100 share(s) held with no resting stop", "seen_at": NOW - dt.timedelta(hours=1)}]
        rows = L.build_rows(positions, gtts=R.gtt_index([gtt("1", "GOOD", 100)]), issues=issues, now=NOW)
        assert [r.symbol for r in rows] == ["NAKEDCO", "GOOD"]
        naked = rows[0]
        assert naked.stop_state == L.NAKED and naked.overdue and not naked.resolved
        assert naked.issues == ("NAKED: 100 share(s) held with no resting stop",)
        assert "arm the stop" in naked.next_action

    def test_page_renders_the_rows_and_says_when_the_broker_was_not_checked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from fastapi.testclient import TestClient

        class Store:
            def open_positions(self) -> list[dict]:
                return [position("twt", 7, "PAGECO", gtt_id=None)]

        class Empty:
            def open_positions(self) -> list[dict]:
                return []

        @contextlib.contextmanager
        def sources():  # noqa: ANN202
            yield L.Sources(stores={"swing": Empty(), "twt": Store(), "vbt": Empty()}, gateways={}, gtts=None, issues=[])

        monkeypatch.setattr(L, "desk_sources", sources)
        monkeypatch.setattr(L, "_now", lambda: NOW)
        client = TestClient(M.app, headers={"Origin": "http://testserver:8420"})
        page = client.get("/lifecycle")
        assert page.status_code == 200
        assert "PAGECO" in page.text and "NAKED" in page.text and "not checked: no Kite session" in page.text
        data = client.get("/lifecycle/data").json()
        assert data["unresolved"] == 1 and data["rows"][0]["symbol"] == "PAGECO"


class TestStopState:
    def _one(self, gtt_id: str | None, gtts: list[dict] | None, *, qty: int = 100) -> L.TradeLifecycle:
        rows = L.build_rows(
            {"twt": [position("twt", 1, "X", qty=qty, gtt_id=gtt_id)]},
            gtts=None if gtts is None else R.gtt_index(gtts),
            issues=[],
            now=NOW,
        )
        return rows[0]

    def test_stop_state_armed_when_the_broker_rests_the_open_quantity(self) -> None:
        row = self._one("9", [gtt("9", "X", 100)])
        assert (row.stop_state, row.stop_qty) == (L.ARMED, 100)

    def test_stop_state_missing_when_the_broker_no_longer_lists_it(self) -> None:
        assert self._one("9", []).stop_state == L.GTT_MISSING
        assert self._one("9", [gtt("9", "X", 100, status="cancelled")]).stop_state == L.GTT_MISSING

    def test_stop_state_oversized_and_undersized_name_the_residual(self) -> None:
        over = self._one("9", [gtt("9", "X", 150)])
        under = self._one("9", [gtt("9", "X", 60)])
        assert (over.stop_state, over.stop_qty) == (L.GTT_OVERSIZED, 150)
        assert (under.stop_state, under.stop_qty) == (L.GTT_UNDERSIZED, 60)
        assert "sells what is not there" in over.next_action

    def test_stop_state_triggered_but_unfilled_is_an_unresolved_position(self) -> None:
        row = self._one("9", [gtt("9", "X", 100, status="triggered")])
        assert row.stop_state == L.TRIGGERED_UNFILLED and not row.resolved
        assert "not a fill" in L.__doc__ and "LIMIT order" in row.next_action

    def test_stop_state_unverified_without_a_broker_list_and_naked_without_a_gtt(self) -> None:
        assert self._one("9", None).stop_state == L.UNVERIFIED
        assert self._one(None, []).stop_state == L.NAKED
        assert self._one("DRY-x", []).stop_state == L.ARMED  # a rehearsal's trigger rests nowhere


# --- adoption -----------------------------------------------------------------------------------


class RecordingGateway:
    def __init__(self) -> None:
        self.armed: list[dict] = []

    async def place_gtt_stop(self, **kwargs: object) -> dict:
        self.armed.append(dict(kwargs))
        return {"status": "GTT_PLACED", "gtt_id": 777, "trigger": kwargs.get("trigger")}

    async def place(self, **_: object) -> dict:
        raise AssertionError("adoption must never place a buy")


def with_conn(store):  # noqa: ANN001, ANN201 - the executor suites' doubles, given the two reads adoption needs
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE instrument (id INTEGER PRIMARY KEY, symbol TEXT)")
    conn.execute("INSERT INTO instrument (id, symbol) VALUES (42, 'HELDCO'), (43, 'NEWCO')")
    conn.execute(
        "CREATE TABLE lv_adoption (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, sleeve TEXT, position_id INTEGER, "
        "symbol TEXT, quantity INTEGER, avg_cost NUMERIC, gtt_id TEXT, note TEXT)"
    )
    store.conn = conn
    store.t = lambda table: table
    store.user_id = 1
    return store


def adoption(sleeve_name: str, **over: object) -> L.Adoption:
    base = {"sleeve": sleeve_name, "symbol": "NEWCO", "quantity": 40, "avg_cost": D("250.00"), "gtt_id": None, "setup": "FLAG" if sleeve_name == "swing" else None, "note": "bought by hand"}
    return L.Adoption(**{**base, **over})  # type: ignore[arg-type]


class TestAdopt:
    @pytest.mark.parametrize("sleeve", ["twt", "vbt", "swing"])
    def test_adopt_creates_the_position_on_the_stated_numbers_arms_a_stop_and_records_it(self, sleeve: str) -> None:
        store = with_conn({"twt": twt.MemoryStore, "vbt": vbt.MemoryStore, "swing": swing.MemoryStore}[sleeve]())
        gw = RecordingGateway()
        out = run(L.adopt(store, gw, adoption(sleeve), confirm="true", now=NOW, gtts={}, arm=L.ARM[sleeve]))
        pos = store.position(out["position_id"])
        assert pos["quantity_open"] == 40 and pos["entry_avg"] == D("250.00") and pos["simulated"] is False
        stop = pos.get("stop_price", pos.get("stop"))
        assert stop < D("250.00") and pos["initial_stop"] == stop
        assert pos["gtt_id"] == "777", "armed through the gateway, and linked"
        assert len(gw.armed) == 1 and gw.armed[0]["qty"] == 40 and gw.armed[0]["symbol"] == "NEWCO"
        recorded = store.conn.execute("SELECT sleeve, symbol, quantity, gtt_id, note FROM lv_adoption").fetchall()
        assert [tuple(r) for r in recorded] == [(sleeve, "NEWCO", 40, "777", "bought by hand")]

    def test_adopt_links_a_hand_armed_gtt_instead_of_arming_a_second(self) -> None:
        store = with_conn(vbt.MemoryStore())
        gw = RecordingGateway()
        gtts = R.gtt_index([gtt("5150", "NEWCO", 40, trigger="230")])
        out = run(L.adopt(store, gw, adoption("vbt", gtt_id="5150"), confirm="true", now=NOW, gtts=gtts, arm=L.ARM["vbt"]))
        pos = store.position(out["position_id"])
        assert pos["gtt_id"] == "5150" and pos["stop_price"] == D("230") and gw.armed == []

    def test_adopt_without_confirm_writes_nothing(self) -> None:
        store = with_conn(twt.MemoryStore())
        with pytest.raises(HTTPException) as refused:
            run(L.adopt(store, RecordingGateway(), adoption("twt"), confirm="", now=NOW, gtts={}, arm=L.ARM["twt"]))
        assert refused.value.status_code == 400
        assert store.positions == {} and store.conn.execute("SELECT count(*) FROM lv_adoption").fetchone()[0] == 0

    def test_adopt_refuses_an_unknown_sleeve_a_held_name_and_an_unknown_symbol(self) -> None:
        store = with_conn(twt.MemoryStore())
        twt.a_position(store, instrument_id=42, symbol="HELDCO")
        for kwargs, code in (({"sleeve": "weekly"}, 400), ({"symbol": "HELDCO"}, 409), ({"symbol": "NOSUCH"}, 404)):
            with pytest.raises(HTTPException) as refused:
                run(L.adopt(store, RecordingGateway(), adoption("twt", **kwargs), confirm="true", now=NOW, gtts={}, arm=L.ARM["twt"]))
            assert refused.value.status_code == code, kwargs

    def test_adopt_into_swing_names_its_setup(self) -> None:
        store = with_conn(swing.MemoryStore())
        with pytest.raises(HTTPException) as refused:
            run(L.adopt(store, RecordingGateway(), adoption("swing", setup=None), confirm="true", now=NOW, gtts={}, arm=L.ARM["swing"]))
        assert refused.value.status_code == 400 and "setup" in refused.value.detail


# --- the card, and the page's one write ----------------------------------------------------------


class TestTheTradeCard:
    def test_card_rules_come_from_the_configs_and_name_the_numbers(self) -> None:
        # LV9 (Maulik, 28 Sep 2026): Qullamaggie's exits on all three, each over its own hard stop.
        assert "20% initial stop" in EXIT_RULES["twt"] and "1/3 sold" in EXIT_RULES["twt"]
        assert "12% initial stop" in EXIT_RULES["vbt"] and "20-day MA" in EXIT_RULES["vbt"]
        assert "1/3 sold" in EXIT_RULES["swing"] and "10-day MA" in EXIT_RULES["swing"]
        assert "Qullamaggie" in EXIT_RULES["twt"] and "Qullamaggie" in EXIT_RULES["vbt"]
        for rule in EXIT_RULES.values():
            assert "target" not in rule.replace("no target", "")

    @pytest.mark.parametrize("page", ["twt.html", "vbt.html", "swing.html"])
    def test_card_shows_initial_stop_rupee_risk_and_the_exit_rule(self, page: str) -> None:
        text = (TEMPLATES / page).read_text()
        assert "exit_rule" in text and "₹ at risk" in text
        assert "initial" in text.lower()


class TestReadonly:
    def test_readonly_the_page_has_one_write_and_it_is_adopt(self) -> None:
        routes = {(r.path, tuple(sorted(r.methods))) for r in L.router.routes}  # type: ignore[attr-defined]
        assert routes == {("/lifecycle", ("GET",)), ("/lifecycle/data", ("GET",)), ("/lifecycle/adopt", ("POST",))}

    def test_no_order_verb_in_the_module(self) -> None:
        source = inspect.getsource(L)
        for verb in ("gateway.place(", ".place_order(", ".cancel_order(", ".delete_gtt(", ".modify_gtt_quantity(", "kc."):
            assert verb not in source, verb
