"""VB6's execute logic, driven through the REAL gateway against a broker that explodes.

Every test here builds its gateway with ``build_vbt_gateway`` over :class:`ExplodingKC`, whose
``place_order``, ``place_gtt``, ``delete_gtt``, ``cancel_order`` and ``instruments`` raise. The
sleeve's execution flag is false (its default), so the whole path — guards, risk, rate limits,
idempotency, journal — runs and ends in the gateway's dry-run branch. If any test here ever
reached a broker it would fail with "reached the broker", which is
``docs/vbt/06`` VB6's acceptance criterion stated as a test: **0 orders reach a broker.**

The store is an in-memory dict implementing the ``VbtStore`` protocol, so these tests assert
``docs/vbt/02`` (the law) and ``04`` §6, §7, §9 (the numbers) rather than any SQL.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app import config as C
from app import vbt_execute as X
from app.core.risk import RiskManager

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
NOW = dt.datetime(2026, 9, 9, 21, 20, tzinfo=IST)
SESSION = dt.date(2026, 9, 9)
D = Decimal


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

    def cancel_order(self, **_: object) -> str:
        raise AssertionError("a cancel reached the broker")

    def instruments(self, *_: object) -> list:
        raise AssertionError("the instrument dump was fetched — a live path ran")


class MemoryStore:
    """The ``VbtStore`` protocol over dicts. Deliberately dumb: the rules are in the module."""

    def __init__(self) -> None:
        self.plans: dict[str, dict] = {}
        self.lines: dict[int, dict] = {}
        self.positions: dict[int, dict] = {}
        self.orders: dict[int, dict] = {}
        self.fills: list[dict] = []
        self.sessions: dict[dt.date, dict] = {}
        self.locked: list[dt.date] = []
        self._next = 100

    def _id(self) -> int:
        self._next += 1
        return self._next

    # --- the protocol ---------------------------------------------------------------
    def plan(self, plan_id: str) -> dict | None:
        return self.plans.get(str(plan_id))

    def line(self, line_id: int) -> dict | None:
        return self.lines.get(int(line_id))

    def set_line(
        self,
        line_id: int,
        *,
        state: str,
        journal_ref: str | None = None,
        order_id: int | None = None,
        position_id: int | None = None,
        note: str | None = None,
    ) -> None:
        row = self.lines[int(line_id)]
        row["state"] = state
        if journal_ref is not None:
            row["journal_ref"] = journal_ref
        if order_id is not None:
            row["order_id"] = order_id
        if position_id is not None:
            row["position_id"] = position_id
        if note is not None:
            row["note"] = note

    def open_position_for(self, instrument_id: int) -> dict | None:
        for row in self.positions.values():
            if (
                row["instrument_id"] == instrument_id
                and row["state"] == "OPEN"
                and row["quantity_open"] > 0
            ):
                return row
        return None

    def position(self, position_id: int) -> dict | None:
        return self.positions.get(int(position_id))

    def create_position(self, fields: dict) -> int:
        identifier = self._id()
        self.positions[identifier] = {"id": identifier, "gtt_id": None, **fields}
        return identifier

    def update_position(self, position_id: int, fields: dict) -> None:
        self.positions[int(position_id)].update(fields)

    def add_fill(self, fields: dict) -> int:
        identifier = self._id()
        self.fills.append({"id": identifier, **fields})
        return identifier

    def order(self, order_id: int) -> dict | None:
        return self.orders.get(int(order_id))

    def create_order(self, fields: dict) -> int:
        identifier = self._id()
        self.orders[identifier] = {"id": identifier, "filled_quantity": 0, **fields}
        return identifier

    def update_order(self, order_id: int, fields: dict) -> None:
        self.orders[int(order_id)].update(fields)

    def working_order_for(self, instrument_id: int) -> dict | None:
        for row in self.orders.values():
            if row["instrument_id"] == instrument_id and row["state"] in {
                "PROPOSED",
                "CONFIRMED",
                "SENT",
                "PARTIAL",
            }:
                return row
        return None

    def bump_session(
        self, day: dt.date, *, mode: str, confirms: int = 0, fills: int = 0, exits: int = 0
    ) -> None:
        row = self.sessions.setdefault(
            day, {"mode": mode, "confirms": 0, "fills": 0, "exits": 0}
        )
        row["mode"] = mode
        row["confirms"] += confirms
        row["fills"] += fills
        row["exits"] += exits

    @contextlib.contextmanager
    def lock_session_for_update(self, day: dt.date):  # noqa: ANN201 - a context manager
        self.locked.append(day)
        yield

    def entries_taken(self, day: dt.date) -> int:  # noqa: ARG002 - one session in these tests
        return sum(
            1
            for row in self.orders.values()
            if row["state"] in {"CONFIRMED", "SENT", "FILLED"}
        )


def a_store(kind: str = "PLACE_LIMIT", **line_overrides: object) -> tuple[MemoryStore, str, int]:
    store = MemoryStore()
    plan_id = str(uuid.uuid4())
    store.plans[plan_id] = {
        "id": 1,
        "plan_id": plan_id,
        "session_date": SESSION,
        "source": "EVENING",
        "built_at": NOW - dt.timedelta(minutes=5),
        "expires_at": NOW + dt.timedelta(minutes=25),
        "gate": "OPEN",
    }
    line_id = 7
    store.lines[line_id] = {
        "id": line_id,
        "plan_pk": 1,
        "plan_id": plan_id,
        "kind": kind,
        "instrument_id": 42,
        "symbol": "VBTCO",
        "quantity": 1_000,
        "limit_price": D("96.00"),
        "stop_price": D("84.45"),
        "value_inr": D("96000.00"),
        "note": "",
        "state": "PROPOSED",
        "client_id": f"{plan_id}:VBTCO:{kind}",
        "order_id": None,
        "position_id": None,
        **line_overrides,
    }
    return store, plan_id, line_id


def gateway():  # noqa: ANN201 - an OrderGateway
    return X.build_vbt_gateway(ExplodingKC(), RiskManager())


def run(coro):  # noqa: ANN001, ANN201 - the desk's own test idiom
    return asyncio.run(coro)


def confirm(store: MemoryStore, plan_id: str, line_id: int, **kwargs: object):  # noqa: ANN201
    return run(
        X.execute_line(
            store,
            gateway(),
            plan_id=plan_id,
            line_id=line_id,
            confirm=str(kwargs.pop("confirm", "true")),
            now=kwargs.pop("now", NOW),
            **kwargs,
        )
    )


class TestNothingFiresWithoutTheClick:
    """`02` Track C §3, and DECISIONS-VB PACK.2: there is no flag that changes this."""

    def test_a_missing_confirm_is_a_four_hundred(self) -> None:
        store, plan_id, line_id = a_store()
        with pytest.raises(HTTPException) as caught:
            confirm(store, plan_id, line_id, confirm="false")
        assert caught.value.status_code == 400
        assert store.orders == {}

    def test_an_unknown_plan_is_a_four_oh_four(self) -> None:
        store, _plan_id, line_id = a_store()
        with pytest.raises(HTTPException) as caught:
            confirm(store, str(uuid.uuid4()), line_id)
        assert caught.value.status_code == 404

    def test_an_expired_plan_is_a_four_ten(self) -> None:
        """`04` §9.4 — thirty minutes, and the refusal says when it lapsed."""
        store, plan_id, line_id = a_store()
        with pytest.raises(HTTPException) as caught:
            confirm(store, plan_id, line_id, now=NOW + dt.timedelta(minutes=31))
        assert caught.value.status_code == 410
        assert "expired" in caught.value.detail

    def test_a_line_confirmed_twice_is_a_four_oh_nine(self) -> None:
        """A re-posted form cannot send twice, before the gateway's idempotency map is asked."""
        store, plan_id, line_id = a_store()
        confirm(store, plan_id, line_id)
        with pytest.raises(HTTPException) as caught:
            confirm(store, plan_id, line_id)
        assert caught.value.status_code == 409

    def test_the_whole_confirm_runs_under_the_session_lock(self) -> None:
        """`04` §9.4 — two browser tabs cannot confirm past the session cap together."""
        store, plan_id, line_id = a_store()
        confirm(store, plan_id, line_id)
        assert store.locked == [SESSION]


class TestThePlaceLimit:
    def test_it_is_a_limit_buy_at_the_signal_close(self) -> None:
        """`04` §7.1 — the level *is* the strategy; a market order is a different trade."""
        store, plan_id, line_id = a_store()
        outcome = confirm(store, plan_id, line_id)
        assert outcome.status == "SIMULATED"
        assert outcome.simulated is True
        order = next(iter(store.orders.values()))
        assert order["limit_price"] == D("96.00")
        assert order["quantity"] == 1_000
        assert order["simulated"] is True

    def test_the_dry_run_branch_rehearses_the_whole_path(self) -> None:
        """`02` Track B — the twenty DRY_RUN sessions are a rehearsal of *this* code, so the
        simulated fill has to produce the position, the fill row and the stop."""
        store, plan_id, line_id = a_store()
        outcome = confirm(store, plan_id, line_id)
        assert outcome.position_id is not None
        position = store.positions[outcome.position_id]
        assert position["quantity_open"] == 1_000
        assert position["entry_avg"] == D("96.00")
        assert position["initial_stop"] == D("84.45")
        assert position["simulated"] is True
        assert [fill["side"] for fill in store.fills] == ["BUY"]

    def test_every_fill_gets_a_stop_the_same_session(self) -> None:
        """Non-negotiable 4, and it is armed in the same request as the fill that created it."""
        store, plan_id, line_id = a_store()
        outcome = confirm(store, plan_id, line_id)
        assert outcome.gtt is not None
        position = store.positions[outcome.position_id]
        assert position["gtt_trigger"] == D("84.45")
        assert position["gtt_armed_at"] is not None

    def test_a_fourth_entry_in_one_session_is_refused(self) -> None:
        """`04` §5.3 — re-read at the confirm, under the lock, not trusted from the plan."""
        store, plan_id, line_id = a_store()
        for index in range(3):
            store.orders[900 + index] = {
                "id": 900 + index,
                "instrument_id": 500 + index,
                "state": "SENT",
            }
        outcome = confirm(store, plan_id, line_id)
        assert outcome.status == "BLOCKED"
        assert outcome.reason.startswith("SESSION_CAP")
        assert store.lines[line_id]["state"] == "REJECTED"

    def test_a_name_already_held_is_refused(self) -> None:
        store, plan_id, line_id = a_store()
        store.positions[1] = {
            "id": 1,
            "instrument_id": 42,
            "state": "OPEN",
            "quantity_open": 100,
            "stop_price": D("80.00"),
            "entry_avg": D("90.00"),
        }
        outcome = confirm(store, plan_id, line_id)
        assert outcome.status == "BLOCKED"
        assert outcome.reason.startswith("ALREADY_HELD")

    def test_a_name_already_bid_for_is_refused(self) -> None:
        store, plan_id, line_id = a_store()
        store.orders[1] = {"id": 1, "instrument_id": 42, "state": "SENT"}
        outcome = confirm(store, plan_id, line_id)
        assert outcome.status == "BLOCKED"
        assert outcome.reason.startswith("ALREADY_WORKING")


class TestTheSell:
    def _held(self, store: MemoryStore, quantity: int = 1_000) -> None:
        store.positions[1] = {
            "id": 1,
            "instrument_id": 42,
            "symbol": "VBTCO",
            "state": "OPEN",
            "quantity_open": quantity,
            "quantity_entered": quantity,
            "entry_avg": D("96.00"),
            "initial_stop": D("84.45"),
            "stop_price": D("84.45"),
            "gtt_id": "gtt-1",
        }

    def test_it_closes_the_position(self) -> None:
        store, plan_id, line_id = a_store("SELL_AT_OPEN")
        self._held(store)
        outcome = confirm(store, plan_id, line_id)
        assert outcome.status == "SIMULATED"
        assert store.positions[1]["quantity_open"] == 0
        assert store.positions[1]["state"] == "CLOSED"
        assert store.positions[1]["close_reason"] == "EMA_EXIT"
        assert [fill["side"] for fill in store.fills] == ["SELL"]

    def test_a_sell_for_a_name_the_sleeve_does_not_own_is_blocked(self) -> None:
        """`02` Track C §5 — the broker's holdings contain the weekly book too."""
        store, plan_id, line_id = a_store("SELL_AT_OPEN")
        outcome = confirm(store, plan_id, line_id)
        assert outcome.status == "BLOCKED"
        assert "did not buy" in outcome.reason
        assert store.fills == []

    def test_a_sell_for_more_than_it_owns_is_blocked(self) -> None:
        store, plan_id, line_id = a_store("SELL_AT_OPEN")
        self._held(store, quantity=400)
        outcome = confirm(store, plan_id, line_id)
        assert outcome.status == "BLOCKED"
        assert "sells 1000" in outcome.reason
        assert store.positions[1]["quantity_open"] == 400


class TestTheCancel:
    def test_it_is_the_only_way_a_working_order_leaves_the_book(self) -> None:
        store, plan_id, line_id = a_store("CANCEL_LIMIT")
        store.orders[1] = {
            "id": 1,
            "instrument_id": 42,
            "state": "SENT",
            "broker_order_id": None,
        }
        outcome = confirm(store, plan_id, line_id)
        assert outcome.status == "SIMULATED"
        assert store.orders[1]["state"] == "CANCELLED"
        assert store.orders[1]["cancel_reason"] == "EXPIRY_SWEEP"

    def test_nothing_to_cancel_is_blocked_rather_than_silent(self) -> None:
        store, plan_id, line_id = a_store("CANCEL_LIMIT")
        outcome = confirm(store, plan_id, line_id)
        assert outcome.status == "BLOCKED"
        assert "no working order" in outcome.reason


class TestTheReArm:
    def test_a_naked_position_gets_its_stop_back(self) -> None:
        store, plan_id, line_id = a_store("ARM_GTT")
        store.positions[1] = {
            "id": 1,
            "instrument_id": 42,
            "symbol": "VBTCO",
            "state": "OPEN",
            "quantity_open": 500,
            "entry_avg": D("96.00"),
            "stop_price": D("84.45"),
            "gtt_id": None,
        }
        outcome = confirm(store, plan_id, line_id)
        assert outcome.status == "SIMULATED"
        assert outcome.gtt is not None
        assert store.positions[1]["gtt_trigger"] == D("84.45")


class TestTheLaw:
    def test_the_flag_is_false_and_a_confirm_is_simulated(self) -> None:
        assert C.VBT_EXECUTION_ENABLED is False
        assert X.vbt_gates().dry_run is True
        assert X.is_simulated() is True

    def test_this_sleeve_can_never_place_an_intraday_or_option_order(self) -> None:
        """Track C §1 — CNC only, and not read from config: whatever the weekly desk allows."""
        gates = X.vbt_gates()
        assert gates.intraday_enabled is False
        assert gates.options_enabled is False

    def test_there_are_four_executable_kinds_and_none_of_them_shorts(self) -> None:
        assert X.EXECUTABLE_KINDS == frozenset(
            {"PLACE_LIMIT", "SELL_AT_OPEN", "CANCEL_LIMIT", "ARM_GTT"}
        )

    def test_the_module_names_no_broker_method(self) -> None:
        """Law 2: every order goes through the gateway this module is handed."""
        import pathlib

        source = pathlib.Path(X.__file__).read_text(encoding="utf-8")
        for forbidden in ("kc.place_order", "kite.place_order", "place_gtt(", "kc."):
            assert forbidden not in source, forbidden

    def test_no_auto_execute_setting_is_referenced_anywhere(self) -> None:
        """DECISIONS-VB PACK.2 — non-negotiable 1's exception belongs to the swing sleeve."""
        import pathlib

        source = pathlib.Path(X.__file__).read_text(encoding="utf-8").upper()
        assert "AUTO_EXECUTE" not in source.replace("AUTO_EXECUTE`` TO FIND", "")
