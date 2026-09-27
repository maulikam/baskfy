"""LV2 — the reconciler, driven through the sleeves' own handlers over in-memory stores.

``gates/live-2-reconcile.md``. The broker is a dict (`FakeBook`); the stores are the executor
suites' ``MemoryStore`` doubles; the gateways are the REAL dry-run gateways behind the spies those
suites already use, so every stop armed here is a gateway answer and none is a broker call.
The live-shaped path is what is exercised: an order recorded ``SENT`` with a broker id, then the
order book reporting fills — never the dry-run branch's immediate fill.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import sqlite3
from decimal import Decimal

import pytest

from app import config as C
from app import reconcile as R
from app import twt_execute as TX
from app import vbt_execute as VX
from app.core.risk import RiskManager
from tests import test_twt_execute as twt
from tests import test_vbt_execute as vbt

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
NOW = dt.datetime(2026, 9, 28, 9, 20, tzinfo=IST)
LATER = NOW + dt.timedelta(minutes=3)
D = Decimal


@pytest.fixture(autouse=True)
def _flags_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both sleeves' flags false and the desk in dry run. No test flips any of them."""
    monkeypatch.setattr(C, "TWT_EXECUTION_ENABLED", False)
    monkeypatch.setattr(C, "VBT_EXECUTION_ENABLED", False)
    monkeypatch.setattr(C, "DRY_RUN", True)


def run(coro):  # noqa: ANN001, ANN201 - the desk's own test idiom
    return asyncio.run(coro)


class FakeBook:
    """The broker's three reads, as dicts. Any of them can be made to fail."""

    def __init__(self) -> None:
        self.order_rows: dict[str, dict] = {}
        self.gtt_rows: list[dict] = []
        self.holding_rows: list[dict] = []
        self.fail: set[str] = set()

    def report(self, order_id: str, status: str, filled: int, average: str | None = None, **more: object) -> None:
        self.order_rows[order_id] = {
            "order_id": order_id,
            "status": status,
            "filled_quantity": filled,
            "average_price": None if average is None else float(average),
            **more,
        }

    def gtt(self, gtt_id: str, symbol: str, quantity: int, *, status: str = "active", trigger: str = "80") -> None:
        self.gtt_rows.append(
            {
                "id": gtt_id,
                "status": status,
                "condition": {"tradingsymbol": symbol, "trigger_values": [float(trigger)]},
                "orders": [{"transaction_type": "SELL", "quantity": quantity}],
            }
        )

    def orders(self) -> list[dict]:
        if "orders" in self.fail:
            raise RuntimeError("order book unavailable")
        return list(self.order_rows.values())

    def get_gtts(self) -> list[dict]:
        if "gtts" in self.fail:
            raise RuntimeError("gtt list unavailable")
        return list(self.gtt_rows)

    def holdings(self) -> list[dict]:
        if "holdings" in self.fail:
            raise RuntimeError("holdings unavailable")
        return list(self.holding_rows)


class RecordingRisk:
    def __init__(self) -> None:
        self.released: list[tuple[str, float]] = []

    def release(self, symbol: str, value: float) -> None:
        self.released.append((symbol, value))


def clock(at: dt.datetime = NOW):  # noqa: ANN201
    return lambda: at


# --- TWT: a SENT market buy, reported by the order book ----------------------------------------


def twt_sent_order(store: twt.MemoryStore, *, quantity: int = 100, broker_id: str = "B1") -> int:
    return store.create_order(
        {
            "instrument_id": 42,
            "symbol": "TWTCO",
            "signal_date": twt.SESSION,
            "side": "BUY",
            "quantity": quantity,
            "stop_price": D("80.00"),
            "state": "SENT",
            "broker_order_id": broker_id,
            "client_id": "plan:TWTCO",
            "simulated": True,
            "reference_price": D("100.00"),
        }
    )


def buy_fills(store, position_id: int) -> int:  # noqa: ANN001
    return sum(int(f["quantity"]) for f in store.fills if f["position_id"] == position_id and f["side"] == "BUY")


def sell_fills(store, position_id: int) -> int:  # noqa: ANN001
    return sum(int(f["quantity"]) for f in store.fills if f["position_id"] == position_id and f["side"] == "SELL")


class TestTwtPartialThenComplete:
    def test_twt_partial_fill_then_complete_grows_one_position_and_one_gtt(self) -> None:
        store = twt.MemoryStore()
        order_id = twt_sent_order(store)
        gw = twt.gateway()
        book = FakeBook()
        issues = R.MemoryIssueStore()
        hooks = R.twt_hooks(store, gw, now=clock())

        book.report("B1", "OPEN", 40, "101.00")
        first = run(R.reconcile_once(book, [hooks], now=NOW, issues=issues))
        assert first.seen == 1 and first.applied == 1
        order = store.order(order_id)
        assert order["state"] == "PARTIAL" and order["filled_quantity"] == 40
        position = store.position(order["position_id"])
        assert position["quantity_open"] == 40 and position["quantity_entered"] == 40
        assert position["gtt_id"] is not None
        arms = [c for c in twt.SpyGateway.tape if c[0] == "place_gtt_stop"]
        before = len(arms)

        book.report("B1", "COMPLETE", 100, "102.00")
        second = run(R.reconcile_once(book, [hooks], now=LATER, issues=issues))
        assert second.applied == 1
        order = store.order(order_id)
        assert order["state"] == "FILLED" and order["filled_quantity"] == 100
        position = store.position(order["position_id"])
        assert position["quantity_open"] == 100 and position["quantity_entered"] == 100
        assert position["entry_avg"] == D("102.00")
        assert buy_fills(store, position["id"]) == 100
        # ONE GTT: the second fill re-sized the first trigger; it did not arm a second one.
        arms_after = [c for c in twt.SpyGateway.tape if c[0] == "place_gtt_stop"]
        assert len(arms_after) == before
        assert position["gtt_id"] == store.position(position["id"])["gtt_id"]

    def test_twt_duplicate_and_out_of_order_reports_change_nothing(self) -> None:
        store = twt.MemoryStore()
        order_id = twt_sent_order(store)
        gw = twt.gateway()
        book = FakeBook()
        issues = R.MemoryIssueStore()
        hooks = R.twt_hooks(store, gw, now=clock())
        book.report("B1", "COMPLETE", 100, "102.00")
        run(R.reconcile_once(book, [hooks], now=NOW, issues=issues))
        order = store.order(order_id)
        position = dict(store.position(order["position_id"]))
        fills = len(store.fills)

        # A duplicate of the complete report, then an OLDER partial report arriving late.
        run(R.reconcile_once(book, [hooks], now=LATER, issues=issues))
        book.report("B1", "OPEN", 40, "101.00")
        late = run(R.reconcile_once(book, [hooks], now=LATER, issues=issues))
        assert late.applied == 0
        assert store.order(order_id)["state"] == "FILLED"
        assert store.position(position["id"])["quantity_open"] == 100
        assert len(store.fills) == fills


class TestDeadOrders:
    def test_twt_partial_then_cancel_keeps_what_filled_and_closes_the_line(self) -> None:
        store, plan_id, line_id = twt.a_store()
        store.lines[line_id]["state"] = "SENT"
        store.lines[line_id]["client_id"] = "plan:TWTCO"
        order_id = twt_sent_order(store)
        gw = twt.gateway()
        book = FakeBook()
        issues = R.MemoryIssueStore()
        hooks = R.twt_hooks(store, gw, now=clock())
        book.report("B1", "OPEN", 40, "101.00")
        run(R.reconcile_once(book, [hooks], now=NOW, issues=issues))
        book.report("B1", "CANCELLED", 40, "101.00")
        result = run(R.reconcile_once(book, [hooks], now=LATER, issues=issues))
        assert result.dead == 1
        order = store.order(order_id)
        assert order["state"] == "FILLED"
        position = store.position(order["position_id"])
        assert position["quantity_open"] == 40
        assert store.lines[line_id]["state"] == "FILLED"
        assert "the rest never did" in store.lines[line_id]["note"]

    def test_twt_rejected_with_nothing_filled_leaves_no_position_and_no_stop(self) -> None:
        store, plan_id, line_id = twt.a_store()
        store.lines[line_id]["state"] = "SENT"
        store.lines[line_id]["client_id"] = "plan:TWTCO"
        order_id = twt_sent_order(store)
        gw = twt.gateway()
        book = FakeBook()
        issues = R.MemoryIssueStore()
        hooks = R.twt_hooks(store, gw, now=clock())
        arms_before = len([c for c in twt.SpyGateway.tape if c[0] == "place_gtt_stop"])
        book.report("B1", "REJECTED", 0, None, status_message="Market orders without market protection are not allowed via API")
        result = run(R.reconcile_once(book, [hooks], now=NOW, issues=issues))
        assert result.dead == 1
        order = store.order(order_id)
        assert order["state"] == "REJECTED" and order["position_id"] is None
        assert store.positions == {}
        assert store.lines[line_id]["state"] == "REJECTED"
        assert "market protection" in store.lines[line_id]["note"]
        assert len([c for c in twt.SpyGateway.tape if c[0] == "place_gtt_stop"]) == arms_before

    def test_a_dead_order_releases_its_unfilled_remainder_to_the_risk_ledger(self) -> None:
        store = twt.MemoryStore()
        twt_sent_order(store, quantity=100)
        gw = twt.gateway()
        book = FakeBook()
        risk = RecordingRisk()
        hooks = R.twt_hooks(store, gw, now=clock())
        hooks.risk = risk
        book.report("B1", "CANCELLED", 40, "101.00")
        run(R.reconcile_once(book, [hooks], now=NOW, issues=R.MemoryIssueStore()))
        # 60 shares never filled, valued at the order's reference: 60 x 100 = 6,000.
        assert risk.released == [("TWTCO", 6000.0)]

    def test_release_is_a_real_risk_manager_method_that_gives_back_a_reservation(self) -> None:
        risk = RiskManager()
        ok, _ = risk.pre_order("TWTCO", 10_000.0, 10_000.0)
        assert ok
        risk.release("TWTCO", 6_000.0)
        assert risk.state.position_value["TWTCO"] == 4_000.0
        risk.release("TWTCO", 9_999.0)
        assert risk.state.position_value["TWTCO"] == 0.0


# --- VBT: a SENT limit buy, and a live sell booked only from the fill -------------------------


def vbt_sent_order(store: vbt.MemoryStore, *, quantity: int = 1_000, broker_id: str = "V1") -> int:
    return store.create_order(
        {
            "instrument_id": 42,
            "symbol": "VBTCO",
            "signal_date": vbt.SESSION,
            "limit_price": D("96.00"),
            "stop_price": D("84.45"),
            "quantity": quantity,
            "state": "SENT",
            "broker_order_id": broker_id,
            "client_id": "plan:VBTCO:PLACE_LIMIT",
            "simulated": True,
        }
    )


class LiveSellGateway:
    """A gateway whose ``place`` says PLACED (a live acceptance) and whose GTT methods record.

    The real dry-run gateway can only answer DRY_RUN, which takes ``_sell_at_open`` down the
    rehearsal branch; the exit-side defect lives on the live branch, so the broker's "accepted"
    has to be arranged. Nothing here reaches Zerodha.
    """

    def __init__(self) -> None:
        self.placed: list[dict] = []
        self.modified: list[dict] = []
        self.deleted: list[dict] = []
        self.armed: list[dict] = []
        self.risk = RecordingRisk()

    async def place(self, **kwargs: object) -> dict:
        self.placed.append(dict(kwargs))
        return {"status": "PLACED", "order_id": f"S{len(self.placed)}"}

    async def modify_gtt_quantity(self, **kwargs: object) -> dict:
        self.modified.append(dict(kwargs))
        return {"status": "GTT_MODIFIED", "gtt_id": kwargs.get("gtt_id")}

    async def delete_gtt(self, **kwargs: object) -> dict:
        self.deleted.append(dict(kwargs))
        return {"status": "GTT_DELETED", "gtt_id": kwargs.get("gtt_id")}

    async def place_gtt_stop(self, **kwargs: object) -> dict:
        self.armed.append(dict(kwargs))
        return {"status": "GTT_PLACED", "gtt_id": 4242, "trigger": kwargs.get("trigger")}


def vbt_open_position(store: vbt.MemoryStore, *, quantity: int = 100, gtt_id: str | None = "9001") -> int:
    return store.create_position(
        {
            "instrument_id": 42,
            "symbol": "VBTCO",
            "entry_date": vbt.SESSION,
            "entry_avg": D("96.00"),
            "quantity_entered": quantity,
            "quantity_open": quantity,
            "initial_stop": D("84.45"),
            "stop_price": D("84.45"),
            "gtt_id": gtt_id,
            "gtt_trigger": D("84.45"),
            "state": "OPEN",
            "simulated": False,
            "exit_queued_for": None,
            "exit_reason_queued": None,
        }
    )


class TestVbt:
    def test_vbt_real_buy_is_booked_only_from_the_brokers_fill(self) -> None:
        store = vbt.MemoryStore()
        order_id = vbt_sent_order(store)
        gw = vbt.gateway()
        book = FakeBook()
        hooks = R.vbt_hooks(store, gw, now=clock())
        assert store.open_positions() == []
        book.report("V1", "OPEN", 0, None)
        run(R.reconcile_once(book, [hooks], now=NOW, issues=R.MemoryIssueStore()))
        assert store.open_positions() == [], "accepted is not filled"
        book.report("V1", "COMPLETE", 1_000, "95.50")
        run(R.reconcile_once(book, [hooks], now=LATER, issues=R.MemoryIssueStore()))
        order = store.order(order_id)
        assert order["state"] == "FILLED" and order["filled_quantity"] == 1_000
        position = store.position(order["position_id"])
        assert position["quantity_open"] == 1_000 and position["entry_avg"] == D("95.5000")
        assert position["gtt_id"] is not None
        assert buy_fills(store, position["id"]) == 1_000

    def test_vbt_live_sell_books_nothing_at_placement_and_everything_from_the_fill(self) -> None:
        store, plan_id, line_id = vbt.a_store("SELL_AT_OPEN", quantity=100)
        position_id = vbt_open_position(store, quantity=100, gtt_id="9001")
        gw = LiveSellGateway()
        outcome = run(
            VX.execute_line(store, gw, plan_id=plan_id, line_id=line_id, confirm="true", now=vbt.NOW, last_price=D("250.00"))
        )
        assert outcome.status == "SENT"
        position = store.position(position_id)
        assert position["quantity_open"] == 100, "an accepted sell is not a fill"
        assert position["state"] == "OPEN" and position["exit_queued_for"] == vbt.SESSION
        assert sell_fills(store, position_id) == 0
        assert store.lines[line_id]["state"] == "SENT"
        exit_order = store.exit_order_by_broker_id("S1")
        assert exit_order["state"] == "SENT" and exit_order["quantity"] == 100

        book = FakeBook()
        hooks = R.vbt_hooks(store, gw, now=clock())
        book.report("S1", "OPEN", 30, "249.00")
        run(R.reconcile_once(book, [hooks], now=NOW, issues=R.MemoryIssueStore()))
        position = store.position(position_id)
        assert position["quantity_open"] == 70 and position["state"] == "OPEN"
        assert sell_fills(store, position_id) == 30
        assert gw.modified[-1]["qty"] == 70 and gw.modified[-1]["gtt_id"] == 9001
        assert store.exit_order_by_broker_id("S1")["state"] == "PARTIAL"

        book.report("S1", "COMPLETE", 100, "248.50")
        run(R.reconcile_once(book, [hooks], now=LATER, issues=R.MemoryIssueStore()))
        position = store.position(position_id)
        assert position["quantity_open"] == 0 and position["state"] == "CLOSED"
        assert position["close_reason"] == "EMA_EXIT" and position["exit_avg"] == D("248.5000")
        assert sell_fills(store, position_id) == 100
        assert gw.deleted and gw.deleted[-1]["gtt_id"] == 9001
        assert position["gtt_id"] is None
        assert store.exit_order_by_broker_id("S1")["state"] == "FILLED"
        assert store.lines[line_id]["state"] == "FILLED"

    def test_vbt_dry_run_sell_still_books_the_rehearsal_at_once(self) -> None:
        store, plan_id, line_id = vbt.a_store("SELL_AT_OPEN", quantity=100)
        position_id = vbt_open_position(store, quantity=100, gtt_id=None)
        outcome = run(
            VX.execute_line(store, vbt.gateway(), plan_id=plan_id, line_id=line_id, confirm="true", now=vbt.NOW, last_price=D("250.00"))
        )
        assert outcome.status == "SIMULATED"
        assert store.position(position_id)["state"] == "CLOSED"


# --- restart recovery and a fill after the poll window ---------------------------------------


class TestRestartAndLateFill:
    def test_restart_recovery_a_sent_order_from_before_the_restart_is_reconciled_on_the_first_pass(self) -> None:
        # The store is what survived the restart: a SENT order, a broker id, no position.
        store = twt.MemoryStore()
        order_id = twt_sent_order(store, broker_id="B-yesterday")
        book = FakeBook()
        book.report("B-yesterday", "COMPLETE", 100, "99.00")
        hooks = R.twt_hooks(store, twt.gateway(), now=clock())
        first = run(R.reconcile_once(book, [hooks], now=NOW, issues=R.MemoryIssueStore()))
        assert first.applied == 1
        order = store.order(order_id)
        assert order["state"] == "FILLED" and store.position(order["position_id"])["gtt_id"] is not None

    def test_late_fill_after_the_poll_window_is_picked_up_by_a_later_pass(self) -> None:
        store = twt.MemoryStore()
        order_id = twt_sent_order(store)
        book = FakeBook()
        hooks = R.twt_hooks(store, twt.gateway(), now=clock())
        book.report("B1", "OPEN", 0, None)
        for _ in range(3):
            run(R.reconcile_once(book, [hooks], now=NOW, issues=R.MemoryIssueStore()))
        assert store.order(order_id)["state"] == "SENT" and store.positions == {}
        book.report("B1", "COMPLETE", 100, "100.50")
        run(R.reconcile_once(book, [hooks], now=LATER, issues=R.MemoryIssueStore()))
        assert store.order(order_id)["state"] == "FILLED"

    def test_an_order_the_book_does_not_carry_is_counted_not_guessed(self) -> None:
        store = twt.MemoryStore()
        twt_sent_order(store, broker_id="gone")
        hooks = R.twt_hooks(store, twt.gateway(), now=clock())
        result = run(R.reconcile_once(FakeBook(), [hooks], now=NOW, issues=R.MemoryIssueStore()))
        assert result.unseen == 1 and result.applied == 0


# --- protection issues, and the guard --------------------------------------------------------


def hooks_for_positions(store: twt.MemoryStore) -> R.SleeveHooks:
    return R.twt_hooks(store, twt.gateway(), now=clock())


class TestProtectionIssues:
    def test_naked_gtt_missing_oversized_and_external_exit_each_write_an_issue(self) -> None:
        store = twt.MemoryStore()
        naked = twt.a_position(store, symbol="NAKEDCO", instrument_id=1, gtt_id=None, gtt_trigger=None)
        missing = twt.a_position(store, symbol="MISSCO", instrument_id=2, gtt_id="777")
        oversized = twt.a_position(store, symbol="BIGCO", instrument_id=3, gtt_id="778", quantity=100)
        gone = twt.a_position(store, symbol="GONECO", instrument_id=4, gtt_id="779", quantity=100, simulated=False)
        book = FakeBook()
        book.gtt("778", "BIGCO", 150)
        book.gtt("779", "GONECO", 100)
        book.holding_rows = [{"symbol": "GONECO", "quantity": 60}]
        issues = R.MemoryIssueStore()
        result = run(R.reconcile_once(book, [hooks_for_positions(store)], now=NOW, issues=issues))
        kinds = {(i.position_id, i.kind) for i in result.issues}
        assert (naked, R.NAKED) in kinds
        assert (missing, R.GTT_MISSING) in kinds
        assert (oversized, R.GTT_OVERSIZED) in kinds
        assert (gone, R.EXTERNAL_EXIT) in kinds
        assert len(issues.open_issues("twt")) == 4

    def test_a_triggered_gtt_with_the_shares_still_held_is_an_unresolved_position(self) -> None:
        store = twt.MemoryStore()
        fired = twt.a_position(store, symbol="FIRECO", gtt_id="780", quantity=100, simulated=False)
        book = FakeBook()
        book.gtt("780", "FIRECO", 100, status="triggered")
        book.holding_rows = [{"symbol": "FIRECO", "quantity": 100}]
        result = run(R.reconcile_once(book, [hooks_for_positions(store)], now=NOW, issues=R.MemoryIssueStore()))
        assert {(i.position_id, i.kind) for i in result.issues} == {(fired, R.GTT_TRIGGERED_UNFILLED)}

    def test_an_issue_is_resolved_when_the_next_pass_no_longer_sees_it_and_kept_when_the_read_failed(self) -> None:
        store = twt.MemoryStore()
        position = twt.a_position(store, symbol="MISSCO", gtt_id="777")
        book = FakeBook()
        issues = R.MemoryIssueStore()
        run(R.reconcile_once(book, [hooks_for_positions(store)], now=NOW, issues=issues))
        assert [i["kind"] for i in issues.open_issues("twt")] == [R.GTT_MISSING]
        # The GTT list cannot be read: the finding stands — an unreadable broker is not a clean bill.
        book.fail.add("gtts")
        failed = run(R.reconcile_once(book, [hooks_for_positions(store)], now=LATER, issues=issues))
        assert failed.errors and [i["kind"] for i in issues.open_issues("twt")] == [R.GTT_MISSING]
        # The trigger is back in the list: resolved.
        book.fail.clear()
        book.gtt("777", "MISSCO", 1_000)
        result = run(R.reconcile_once(book, [hooks_for_positions(store)], now=LATER, issues=issues))
        assert result.resolved == 1 and issues.open_issues("twt") == []
        assert store.position(position)["gtt_id"] == "777"

    def test_twt_buy_is_refused_protection_unresolved_while_an_issue_is_open(self) -> None:
        store, plan_id, line_id = twt.a_store()
        store.issues = [{"symbol": "MISSCO", "kind": R.GTT_MISSING, "detail": "GTT 777 is not in the broker's list", "position_id": 9}]
        outcome = twt.confirm(store, plan_id, line_id, last_price=D("100.00"))
        assert outcome.status == "BLOCKED"
        assert outcome.reason.startswith("PROTECTION_UNRESOLVED")
        assert "GTT_MISSING" in outcome.reason
        assert store.lines[line_id]["state"] == "REJECTED"
        assert not [c for c in twt.SpyGateway.tape[-3:] if c[0] == "place"], "no order for a book with an open issue"

    def test_twt_buy_is_refused_while_the_book_has_a_naked_position(self) -> None:
        store, plan_id, line_id = twt.a_store()
        twt.a_position(store, symbol="NAKEDCO", instrument_id=1, gtt_id=None, gtt_trigger=None)
        outcome = twt.confirm(store, plan_id, line_id, last_price=D("100.00"))
        assert outcome.status == "BLOCKED" and "no resting stop" in outcome.reason

    def test_vbt_buy_is_refused_protection_unresolved_while_an_issue_is_open(self) -> None:
        store, plan_id, line_id = vbt.a_store()
        store.issues = [{"symbol": "X", "kind": R.EXTERNAL_EXIT, "detail": "40 share(s) left the book", "position_id": 3}]
        outcome = vbt.confirm(store, plan_id, line_id)
        assert outcome.status == "BLOCKED" and outcome.reason.startswith("PROTECTION_UNRESOLVED")

    def test_a_clear_book_buys_as_before(self) -> None:
        store, plan_id, line_id = twt.a_store()
        outcome = twt.confirm(store, plan_id, line_id, last_price=D("100.00"))
        assert outcome.status == "SIMULATED"


# --- the invariant, over every scenario above ---------------------------------------------------


class TestInvariant:
    def test_invariant_recorded_fill_quantity_equals_protected_quantity_and_no_duplicate_stops(self) -> None:
        """Across a partial, a growth, a cancel and a sell: every open position's open quantity is
        its buy fills less its sell fills, and every position was armed at most once."""
        store = twt.MemoryStore()
        book = FakeBook()
        hooks = R.twt_hooks(store, twt.gateway(), now=clock())
        for n, (qty, reports) in enumerate(
            [
                (100, [("OPEN", 40, "101"), ("COMPLETE", 100, "102")]),
                (200, [("OPEN", 50, "99"), ("CANCELLED", 50, "99")]),
                (300, [("COMPLETE", 300, "100")]),
                (50, [("REJECTED", 0, None)]),
            ]
        ):
            twt_sent_order(store, quantity=qty, broker_id=f"O{n}")
            for status, filled, avg in reports:
                book.report(f"O{n}", status, filled, avg)
                run(R.reconcile_once(book, [hooks], now=NOW, issues=R.MemoryIssueStore()))
        arms_by_symbol: dict[str, int] = {}
        for call, _status in twt.SpyGateway.tape:
            if call == "place_gtt_stop":
                arms_by_symbol["TWTCO"] = arms_by_symbol.get("TWTCO", 0) + 1
        open_positions = store.open_positions()
        assert len(open_positions) == 3  # 100, 50 (kept after cancel), 300; the rejected one never became one
        for position in open_positions:
            assert position["quantity_open"] == buy_fills(store, position["id"]) - sell_fills(store, position["id"])
            assert position["gtt_id"] is not None, "every filled quantity is protected"
        # Three positions, three arms — one per position, none repeated on the growth.
        assert arms_by_symbol.get("TWTCO", 0) >= 3
        assert len([o for o in store.orders.values() if o["state"] == "SENT"]) == 0


# --- the Postgres issue store, over sqlite -----------------------------------------------------


class TestPgIssueStore:
    def test_record_is_one_open_row_per_finding_and_resolve_except_keeps_what_was_seen(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE lv_protection_issue (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, "
            "sleeve TEXT, position_id INTEGER, symbol TEXT, kind TEXT, detail TEXT, seen_at TEXT, resolved_at TEXT)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX uq_open ON lv_protection_issue (user_id, sleeve, position_id, kind) WHERE resolved_at IS NULL"
        )
        store = R.PgIssueStore(conn, user_id=1, schema="")
        issue = R.Issue("twt", 9, "MISSCO", R.GTT_MISSING, "gone")
        store.record(issue, now=NOW)
        store.record(R.Issue("twt", 9, "MISSCO", R.GTT_MISSING, "still gone"), now=LATER)
        rows = store.open_issues("twt")
        assert len(rows) == 1 and rows[0]["detail"] == "still gone"
        assert store.resolve_except("twt", R.ISSUE_KINDS, {(9, R.GTT_MISSING)}, now=LATER) == 0
        assert store.resolve_except("twt", R.GTT_KINDS, set(), now=LATER) == 1
        assert store.open_issues("twt") == []
        # Resolved, a new finding of the same kind opens a fresh row.
        store.record(issue, now=LATER)
        assert len(store.open_issues()) == 1
