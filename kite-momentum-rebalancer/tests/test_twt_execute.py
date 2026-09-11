"""TW6's execute logic, driven through the REAL gateway against a broker that explodes.

Every test here builds its gateway with ``build_twt_gateway`` over :class:`ExplodingKC`, whose
``place_order``, ``place_gtt``, ``delete_gtt``, ``cancel_order`` and ``instruments`` raise. The
sleeve's execution flag is false (its default), so the whole path — guards, risk, rate limits,
idempotency, journal — runs and ends in the gateway's dry-run branch. If any test here ever
reached a broker it would fail with "reached the broker", which is ``docs/twt/06`` TW6's
acceptance criterion stated as a test: **0 orders reach a broker.**

The store is an in-memory dict implementing the ``TwtStore`` protocol, so these tests assert
``docs/twt/02`` (the law) and ``04`` §§6, 7, 10 (the numbers) rather than any SQL.

WHAT IS ASSERTED HERE THAT IS NOT ASSERTED ANYWHERE ELSE
--------------------------------------------------------
**The two failure halves of the ratchet.** A dry-run gateway always succeeds, so the two cases
that matter are driven through a gateway that fails on purpose — one that refuses the cancel and
one that refuses the arm. They are the two halves of ``RAISE_GTT_STOP``, they fail differently,
and the second one leaves a live line with no stop. Nothing else in this repository can produce
that state on demand, so nothing else can prove it is handled.
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
from app import twt_execute as X
from app.core.risk import RiskManager

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
NOW = dt.datetime(2026, 9, 11, 9, 20, tzinfo=IST)
SESSION = dt.date(2026, 9, 10)
D = Decimal

#: ₹25 lakh over ten slots is a ₹2.5 lakh line — ``04`` §3.5's own worked example.
TWENTY_FIVE_LAKH = D("2500000.00")
#: A name turning over ₹50 crore a day, where the 1 %-of-turnover cap does not bind.
FIFTY_CRORE = D("500000000")


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

    def delete_gtt(self, *_: object, **__: object) -> None:
        raise AssertionError("a GTT cancel reached the broker")

    def cancel_order(self, **_: object) -> str:
        raise AssertionError("a cancel reached the broker")

    def instruments(self, *_: object) -> list:
        raise AssertionError("the instrument dump was fetched — a live path ran")


class MemoryStore:
    """The ``TwtStore`` protocol over dicts. Deliberately dumb: the rules are in the module."""

    def __init__(self, *, capital: Decimal = TWENTY_FIVE_LAKH, first_live: int = 10) -> None:
        self.plans: dict[str, dict] = {}
        self.lines: dict[int, dict] = {}
        self.positions: dict[int, dict] = {}
        self.orders: dict[int, dict] = {}
        self.fills: list[dict] = []
        self.sessions: dict[dt.date, dict] = {}
        self.audit: list[dict] = []
        self.locked: list[dt.date] = []
        self.counted: list[int] = []
        self.config: dict = {
            "sleeve_capital_inr": capital,
            "max_open_positions": 10,
            "max_position_pct": D("12.50"),
            "stop_pct": D("20.00"),
            "trail_pct": D("20.00"),
            "first_live_entries_left": first_live,
        }
        self._next = 100

    def _id(self) -> int:
        self._next += 1
        return self._next

    # --- the protocol --------------------------------------------------------------
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
        for key, value in (
            ("journal_ref", journal_ref),
            ("order_id", order_id),
            ("position_id", position_id),
            ("note", note),
        ):
            if value is not None:
                row[key] = value

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

    def open_positions(self) -> list[dict]:
        return [
            row
            for row in self.positions.values()
            if row["state"] == "OPEN" and row["quantity_open"] > 0
        ]

    def create_position(self, fields: dict) -> int:
        identifier = self._id()
        self.positions[identifier] = {
            "id": identifier,
            "gtt_id": None,
            "gtt_trigger": None,
            "half_size": False,
            "symbol": fields.get("symbol", "TWTCO"),
            **fields,
        }
        return identifier

    def update_position(self, position_id: int, fields: dict) -> None:
        self.positions[int(position_id)].update(fields)

    def add_fill(self, fields: dict) -> int:
        identifier = self._id()
        self.fills.append({"id": identifier, **fields})
        return identifier

    def order(self, order_id: int) -> dict | None:
        return self.orders.get(int(order_id))

    def order_by_broker_id(self, broker_order_id: str) -> dict | None:
        for row in self.orders.values():
            if str(row.get("broker_order_id")) == str(broker_order_id):
                return row
        return None

    def create_order(self, fields: dict) -> int:
        identifier = self._id()
        self.orders[identifier] = {
            "id": identifier,
            "filled_quantity": 0,
            "position_id": None,
            "symbol": "TWTCO",
            **fields,
        }
        return identifier

    def update_order(self, order_id: int, fields: dict) -> None:
        self.orders[int(order_id)].update(fields)

    def entries_taken(self, day: dt.date) -> int:
        return sum(
            1
            for row in self.orders.values()
            if row.get("side") == "BUY"
            and row.get("signal_date") == day
            and row["state"] in {"CONFIRMED", "SENT", "PARTIAL", "FILLED"}
        )

    def bump_session(  # noqa: PLR0913 - the session row is its counters
        self,
        day: dt.date,
        *,
        mode: str,
        confirms: int = 0,
        fills: int = 0,
        ratchets: int = 0,
        exits: int = 0,
    ) -> None:
        row = self.sessions.setdefault(
            day,
            {"mode": mode, "confirms": 0, "fills": 0, "ratchets": 0, "exits": 0, "naked": 0},
        )
        row["mode"] = mode
        row["confirms"] += confirms
        row["fills"] += fills
        row["ratchets"] += ratchets
        row["exits"] += exits

    def set_naked_count(self, day: dt.date, *, mode: str, naked: int) -> None:
        row = self.sessions.setdefault(
            day,
            {"mode": mode, "confirms": 0, "fills": 0, "ratchets": 0, "exits": 0, "naked": 0},
        )
        row["naked"] = naked

    @contextlib.contextmanager
    def lock_session_for_update(self, day: dt.date):  # noqa: ANN201 - a context manager
        self.locked.append(day)
        yield

    def sleeve_money(self, as_of: dt.date):  # noqa: ANN201, ARG002 - a SleeveMoney
        return X.sleeve_money_from_rows(self.config, self.open_positions(), {}, D(0))

    def adj_factor(self, instrument_id: int, as_of: dt.date) -> Decimal:  # noqa: ARG002
        return D(1)

    def count_first_live_entry(
        self, position_id: int, *, session_date: dt.date, now: dt.datetime, live: bool
    ) -> bool:
        position = self.positions[int(position_id)]
        if position.get("half_size") or not live or position.get("simulated"):
            return False
        if int(self.config["first_live_entries_left"]) <= 0:
            return False
        self.config["first_live_entries_left"] -= 1
        position["half_size"] = True
        self.counted.append(int(position_id))
        self.audit.append(
            {"key": "first_live_entries_left", "at": now, "session_date": session_date}
        )
        return True

    def expire_plans(self, now: dt.datetime) -> int:
        expired = 0
        for plan in self.plans.values():
            if plan["expires_at"] > now:
                plan["expires_at"] = now
                expired += 1
        return expired

    def zero_capital(self, *, now: dt.datetime, changed_by: str) -> Decimal | None:
        before = self.config["sleeve_capital_inr"]
        self.audit.append(
            {
                "key": "sleeve_capital_inr",
                "old_value": str(before),
                "new_value": "0",
                "at": now,
                "changed_by": changed_by,
            }
        )
        self.config["sleeve_capital_inr"] = D(0)
        return before


def a_store(
    kind: str = "BUY_AT_OPEN", *, capital: Decimal = TWENTY_FIVE_LAKH, **line_overrides: object
) -> tuple[MemoryStore, str, int]:
    store = MemoryStore(capital=capital)
    plan_id = str(uuid.uuid4())
    store.plans[plan_id] = {
        "id": 1,
        "plan_id": plan_id,
        "session_date": SESSION,
        "source": "MORNING",
        "built_at": NOW - dt.timedelta(minutes=5),
        "expires_at": NOW + dt.timedelta(minutes=25),
        "gate": "OPEN",
    }
    line_id = 7
    suffix = "" if kind == "BUY_AT_OPEN" else f":{kind}"
    store.lines[line_id] = {
        "id": line_id,
        "plan_pk": 1,
        "plan_id": plan_id,
        "kind": kind,
        "instrument_id": 42,
        "symbol": "TWTCO",
        "quantity": 2_500,
        "stop_price": D("80.00"),
        "value_inr": D("250000.00"),
        "high_since": None,
        "previous_trigger": None,
        "note": "",
        "state": "PROPOSED",
        "client_id": f"{plan_id}:TWTCO{suffix}",
        "order_id": None,
        "position_id": None,
        "session_date": SESSION,
        "turnover_avg_inr": FIFTY_CRORE,
        **line_overrides,
    }
    return store, plan_id, line_id


def a_position(  # noqa: PLR0913 - a book row is its numbers
    store: MemoryStore,
    *,
    instrument_id: int = 42,
    symbol: str = "TWTCO",
    quantity: int = 1_000,
    entry: str = "100.00",
    stop: str = "80.00",
    gtt_id: str | None = "551234",
    gtt_trigger: str | None = "80.00",
    simulated: bool = True,
) -> int:
    identifier = store._id()  # noqa: SLF001 - the fixture owns the store
    store.positions[identifier] = {
        "id": identifier,
        "instrument_id": instrument_id,
        "symbol": symbol,
        "signal_date": SESSION,
        "entry_date": SESSION,
        "entry_avg": D(entry),
        "entry_adj_factor": D(1),
        "quantity_entered": quantity,
        "quantity_open": quantity,
        "initial_stop": D(stop),
        "stop_price": D(stop),
        "high_since": D(entry),
        "gtt_id": gtt_id,
        "gtt_trigger": None if gtt_trigger is None else D(gtt_trigger),
        "next_trigger": None,
        "next_trigger_for": None,
        "state": "OPEN",
        "simulated": simulated,
        "half_size": False,
    }
    return identifier


class SpyGateway:
    """A real gateway with a tape. **This is the G7 spy.**

    It records the status of every ``place``, ``place_gtt_stop`` and ``delete_gtt`` this module
    makes, so the claim "0 orders reach a broker" is asserted from the gateway's own *answers*
    rather than inferred from reading the code — and it asserts **on every call**, not once at
    the end, so the guarantee does not depend on which order pytest happens to run the tests in.

    With ``BASKFY_TWT_EXECUTION_ENABLED`` false, every answer must be a dry-run status. A single
    ``PLACED`` would be a real order, and it would fail here one line after it happened.
    """

    #: Shared across the module, so the closing test can say the spy was actually exercised.
    tape: list[tuple[str, str]] = []

    def __init__(self, inner: object) -> None:
        self.inner = inner

    def _record(self, call: str, result: dict) -> dict:
        status = str(result.get("status"))
        SpyGateway.tape.append((call, status))
        assert status.startswith("DRY_RUN"), (
            f"{call} answered {status!r} with BASKFY_TWT_EXECUTION_ENABLED false — that is a "
            f"real order, and this suite must place none"
        )
        return result

    async def place(self, **kwargs: object) -> dict:
        return self._record("place", await self.inner.place(**kwargs))

    async def place_gtt_stop(self, **kwargs: object) -> dict:
        return self._record("place_gtt_stop", await self.inner.place_gtt_stop(**kwargs))

    async def delete_gtt(self, **kwargs: object) -> dict:
        return self._record("delete_gtt", await self.inner.delete_gtt(**kwargs))


def gateway():  # noqa: ANN201 - an OrderGateway behind the spy
    return SpyGateway(X.build_twt_gateway(ExplodingKC(), RiskManager()))


class FlakyGateway:
    """A gateway whose two GTT halves can be made to fail one at a time.

    The real dry-run gateway always succeeds, so the two states that matter — "cancelled and
    still resting" and "cancelled and not re-armed" — cannot be reached through it. This is not
    a stand-in for the gateway's logic (the tests above use the real one); it is a way to make
    the *broker* say no, which is the only thing a test cannot arrange for real.
    """

    def __init__(self, *, cancel_ok: bool = True, arm_ok: bool = True) -> None:
        self.cancel_ok = cancel_ok
        self.arm_ok = arm_ok
        self.armed: list[dict] = []
        self.cancelled: list[dict] = []

    async def place(self, **_: object) -> dict:
        raise AssertionError("the flaky gateway is for GTTs; no buy should reach it")

    async def delete_gtt(self, **kwargs: object) -> dict:
        self.cancelled.append(dict(kwargs))
        if self.cancel_ok:
            return {"status": "GTT_DELETED", "gtt_id": kwargs.get("gtt_id")}
        return {"status": "GTT_DELETE_ERROR", "error": "the exchange refused the cancel"}

    async def place_gtt_stop(self, **kwargs: object) -> dict:
        self.armed.append(dict(kwargs))
        if self.arm_ok:
            return {"status": "GTT_PLACED", "gtt_id": 998877, "trigger": kwargs.get("trigger")}
        return {"status": "GTT_ERROR", "error": "the exchange refused the trigger"}


def run(coro):  # noqa: ANN001, ANN201 - the desk's own test idiom
    return asyncio.run(coro)


def confirm(store: MemoryStore, plan_id: str, line_id: int, **kwargs: object):  # noqa: ANN201
    return run(
        X.execute_line(
            store,
            kwargs.pop("gateway", None) or gateway(),
            plan_id=plan_id,
            line_id=line_id,
            confirm=str(kwargs.pop("confirm", "true")),
            now=kwargs.pop("now", NOW),
            **kwargs,
        )
    )


@pytest.fixture(autouse=True)
def _flag_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sleeve's flag is false and the desk is in dry run. **No test flips either.**"""
    monkeypatch.setattr(C, "TWT_EXECUTION_ENABLED", False)
    monkeypatch.setattr(C, "DRY_RUN", True)


# =========================================================================================
# G2 — nothing fires without the click
# =========================================================================================
class TestNothingFiresWithoutTheClick:
    """Non-negotiable 1 and ``02`` Track C §3: there is no flag that changes this."""

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
        assert store.orders == {}

    def test_an_expired_plan_is_a_four_ten_and_nothing_is_sent(self) -> None:
        """``04`` §10.4 — thirty minutes, and the refusal says when it lapsed."""
        store, plan_id, line_id = a_store()
        with pytest.raises(HTTPException) as caught:
            confirm(store, plan_id, line_id, now=NOW + dt.timedelta(minutes=31))
        assert caught.value.status_code == 410
        assert "expired" in caught.value.detail
        assert store.orders == {}

    def test_a_plan_expired_by_one_second_is_still_expired(self) -> None:
        store, plan_id, line_id = a_store()
        with pytest.raises(HTTPException) as caught:
            confirm(store, plan_id, line_id, now=NOW + dt.timedelta(minutes=25, seconds=1))
        assert caught.value.status_code == 410

    def test_a_sell_at_open_line_cannot_be_confirmed_at_all(self) -> None:
        """``04`` §10.2 and DECISIONS-TW TW6.4 — this sleeve has no end-of-day sell rule."""
        store, plan_id, line_id = a_store(kind="SELL_AT_OPEN")
        with pytest.raises(HTTPException) as caught:
            confirm(store, plan_id, line_id)
        assert caught.value.status_code == 400
        assert "the GTT is the exit" in caught.value.detail

    def test_the_whole_confirm_runs_under_the_session_lock(self) -> None:
        """``04`` §10.5 — two browser tabs cannot confirm past the session cap together."""
        store, plan_id, line_id = a_store()
        confirm(store, plan_id, line_id, last_price=D("100.00"))
        assert store.locked == [SESSION]


# =========================================================================================
# G6 — a line is confirmed once, and the key is `plan_id:symbol`
# =========================================================================================
class TestTheIdempotencyKey:
    def test_a_second_confirm_of_the_same_line_is_refused(self) -> None:
        """A re-posted form cannot double-send, before the gateway's map is even asked."""
        store, plan_id, line_id = a_store()
        confirm(store, plan_id, line_id, last_price=D("100.00"))
        with pytest.raises(HTTPException) as caught:
            confirm(store, plan_id, line_id, last_price=D("100.00"))
        assert caught.value.status_code == 409
        assert len(store.orders) == 1

    def test_the_orders_client_id_is_plan_id_colon_symbol(self) -> None:
        """Non-negotiable 6, ``04`` §10.4 — no kind suffix on an order."""
        store, plan_id, line_id = a_store()
        confirm(store, plan_id, line_id, last_price=D("100.00"))
        order = next(iter(store.orders.values()))
        assert order["client_id"] == f"{plan_id}:TWTCO"

    def test_a_gtt_legs_client_id_carries_its_kind(self) -> None:
        """``plan_id:symbol:kind`` for a GTT leg, so a raise and an arm cannot collide."""
        store, plan_id, line_id = a_store(kind="ARM_GTT")
        a_position(store, gtt_id=None, gtt_trigger=None)
        confirm(store, plan_id, line_id, last_price=D("100.00"))
        assert store.lines[line_id]["client_id"] == f"{plan_id}:TWTCO:ARM_GTT"
        assert store.lines[line_id]["state"] == "FILLED"


# =========================================================================================
# G5 — every fill arms a GTT in the same request, and the fourth entry is refused
# =========================================================================================
class TestTheEntry:
    def test_a_fill_arms_gtt_in_the_same_request(self) -> None:
        """Non-negotiable 4, and on this sleeve it is also ``04`` §7.4's fill-day rule."""
        store, plan_id, line_id = a_store()
        outcome = confirm(store, plan_id, line_id, last_price=D("100.00"))

        assert outcome.status == "SIMULATED"
        assert outcome.position_id is not None
        position = store.positions[outcome.position_id]
        assert position["gtt_id"] is not None
        assert position["gtt_trigger"] == D("80.00")  # 20 % under the fill, on the tick
        assert position["initial_stop"] == D("80.00")
        assert position["high_since"] == D("100.00")
        assert [fill["side"] for fill in store.fills] == ["BUY"]

    def test_the_stop_comes_from_the_fill_and_not_from_the_plans_preview(self) -> None:
        """``04`` §7.1 and §5.3 — measured from the exchange's price, not last night's."""
        store, plan_id, line_id = a_store()
        outcome = confirm(store, plan_id, line_id, last_price=D("120.00"))
        position = store.positions[outcome.position_id]
        assert position["entry_avg"] == D("120.00")
        assert position["initial_stop"] == D("96.00")

    def test_a_fourth_entry_in_one_session_is_blocked_by_the_session_cap(self) -> None:
        """``04`` §6.3 — and it says ``SESSION_CAP`` rather than just being short."""
        store, plan_id, line_id = a_store()
        for index in range(3):
            store.orders[900 + index] = {
                "id": 900 + index,
                "side": "BUY",
                "signal_date": SESSION,
                "state": "FILLED",
                "instrument_id": 500 + index,
            }
        outcome = confirm(store, plan_id, line_id, last_price=D("100.00"))

        assert outcome.status == "BLOCKED"
        assert outcome.reason.startswith("SESSION_CAP")
        assert store.lines[line_id]["state"] == "REJECTED"
        assert len(store.orders) == 3

    def test_the_session_cap_counts_orders_from_any_plan(self) -> None:
        """"Whatever plan they came from" is the whole content of the rule."""
        store, plan_id, line_id = a_store()
        store.orders[900] = {
            "id": 900,
            "side": "BUY",
            "signal_date": SESSION,
            "state": "CONFIRMED",
            "instrument_id": 77,
        }
        assert store.entries_taken(SESSION) == 1
        outcome = confirm(store, plan_id, line_id, last_price=D("100.00"))
        assert outcome.status == "SIMULATED"

    def test_a_name_already_held_is_never_averaged_down(self) -> None:
        store, plan_id, line_id = a_store()
        a_position(store)
        outcome = confirm(store, plan_id, line_id, last_price=D("100.00"))
        assert outcome.status == "BLOCKED"
        assert outcome.reason.startswith("ALREADY_HELD")

    def test_a_sleeve_at_zero_blocks_the_confirm_too(self) -> None:
        """``04`` §9.3 and §10.5 — the confirm re-sizes through the same rules the plan did,
        which is what makes zeroing the capital a working stop button."""
        store, plan_id, line_id = a_store(capital=D("0.00"))
        outcome = confirm(store, plan_id, line_id, last_price=D("100.00"))
        assert outcome.status == "BLOCKED"
        assert outcome.reason.startswith("NO_SLEEVE_CAPITAL")
        assert store.orders == {}

    def test_a_line_is_never_grown_past_what_the_page_showed(self) -> None:
        """``04`` §10.5 — a confirm that sent more than the page said is a different order."""
        store, plan_id, line_id = a_store(quantity=100, value_inr=D("10000.00"))
        outcome = confirm(store, plan_id, line_id, last_price=D("100.00"))
        assert outcome.status == "SIMULATED"
        assert store.positions[outcome.position_id]["quantity_open"] == 100

    def test_the_countdown_does_not_move_on_a_simulated_fill(self) -> None:
        """``04`` §6.4, TW5.3 — half size is a live-money discipline."""
        store, plan_id, line_id = a_store()
        confirm(store, plan_id, line_id, last_price=D("100.00"))
        assert store.config["first_live_entries_left"] == 10
        assert store.counted == []


# =========================================================================================
# G3 — a stop never falls
# =========================================================================================
class TestAStopNeverFalls:
    def test_a_stop_never_falls_a_raise_at_the_resting_trigger_is_blocked(self) -> None:
        """``04`` §7.2 — and **equal is refused too**: cancelling and re-arming the same level
        is a moment of nakedness for nothing."""
        store, plan_id, line_id = a_store(kind="RAISE_GTT_STOP", stop_price=D("80.00"))
        a_position(store, gtt_trigger="80.00")
        flaky = FlakyGateway()

        outcome = confirm(
            store, plan_id, line_id, last_price=D("130.00"), gateway=flaky
        )

        assert outcome.status == "BLOCKED"
        assert "a stop never falls" in outcome.reason
        assert flaky.cancelled == [] and flaky.armed == []

    def test_a_new_stop_below_the_resting_one_is_blocked(self) -> None:
        store, plan_id, line_id = a_store(kind="RAISE_GTT_STOP", stop_price=D("70.00"))
        a_position(store, gtt_trigger="80.00")
        flaky = FlakyGateway()
        outcome = confirm(store, plan_id, line_id, last_price=D("130.00"), gateway=flaky)
        assert outcome.status == "BLOCKED"
        assert "a stop never falls" in outcome.reason
        assert flaky.cancelled == []

    def test_a_stop_never_falls_below_the_higher_of_the_stop_and_the_gtt(self) -> None:
        """TW4.8 — the two can differ while a raise is in flight, and the safe reading is the
        higher of them."""
        store, plan_id, line_id = a_store(kind="RAISE_GTT_STOP", stop_price=D("95.00"))
        a_position(store, stop="96.00", gtt_trigger="80.00")
        flaky = FlakyGateway()
        outcome = confirm(store, plan_id, line_id, last_price=D("130.00"), gateway=flaky)
        assert outcome.status == "BLOCKED"
        assert "96.00" in outcome.reason

    def test_a_trigger_at_or_above_last_price_is_blocked(self) -> None:
        """A trigger at or above the last traded price fires the moment it is armed, which on a
        GTT means selling the position at the next tick for no reason."""
        store, plan_id, line_id = a_store(kind="RAISE_GTT_STOP", stop_price=D("130.00"))
        a_position(store, gtt_trigger="80.00")
        flaky = FlakyGateway()

        outcome = confirm(store, plan_id, line_id, last_price=D("130.00"), gateway=flaky)

        assert outcome.status == "BLOCKED"
        assert "would fire at once" in outcome.reason
        assert flaky.cancelled == [] and flaky.armed == []

    def test_a_trigger_with_no_last_price_to_check_it_above_last_is_blocked(self) -> None:
        store, plan_id, line_id = a_store(kind="RAISE_GTT_STOP", stop_price=D("104.00"))
        a_position(store, gtt_trigger="80.00")
        flaky = FlakyGateway()
        outcome = confirm(store, plan_id, line_id, gateway=flaky)
        assert outcome.status == "BLOCKED"
        assert "no last price" in outcome.reason
        assert flaky.cancelled == []

    def test_a_good_raise_moves_the_stop_and_clears_the_spent_trigger(self) -> None:
        store, plan_id, line_id = a_store(kind="RAISE_GTT_STOP", stop_price=D("104.00"))
        position_id = a_position(store, gtt_trigger="80.00")
        store.positions[position_id]["next_trigger"] = D("104.00")
        store.positions[position_id]["next_trigger_for"] = SESSION
        flaky = FlakyGateway()

        outcome = confirm(store, plan_id, line_id, last_price=D("130.00"), gateway=flaky)

        assert outcome.status == "SIMULATED"
        position = store.positions[position_id]
        assert position["stop_price"] == D("104.00")
        assert position["gtt_trigger"] == D("104.00")
        assert position["gtt_id"] == "998877"
        assert position["next_trigger"] is None
        assert store.sessions[SESSION]["ratchets"] == 1
        # Delete first, then arm. Never two triggers on one position at the same time.
        assert len(flaky.cancelled) == 1 and len(flaky.armed) == 1


# =========================================================================================
# G4 — the delete-and-replace path's two failure halves
# =========================================================================================
class TestTheTwoFailureHalves:
    def test_when_the_cancel_fails_the_old_stop_is_still_resting(self) -> None:
        """**Place nothing.** Two triggers sell the position twice when they fire; a stop one
        session behind is a nuisance and not a risk."""
        store, plan_id, line_id = a_store(kind="RAISE_GTT_STOP", stop_price=D("104.00"))
        position_id = a_position(store, gtt_id="551234", gtt_trigger="80.00")
        flaky = FlakyGateway(cancel_ok=False)

        outcome = confirm(store, plan_id, line_id, last_price=D("130.00"), gateway=flaky)

        assert outcome.status == "BLOCKED"
        assert outcome.naked is False
        assert "still resting" in outcome.reason
        position = store.positions[position_id]
        assert position["gtt_id"] == "551234"
        assert position["gtt_trigger"] == D("80.00")
        assert position["stop_price"] == D("80.00")
        assert flaky.armed == []  # nothing was placed

    def test_when_the_arm_fails_after_a_good_cancel_the_line_is_named_naked(self) -> None:
        """**The state that can actually cost money**, so it is named rather than swallowed."""
        store, plan_id, line_id = a_store(kind="RAISE_GTT_STOP", stop_price=D("104.00"))
        position_id = a_position(store, gtt_id="551234", gtt_trigger="80.00")
        flaky = FlakyGateway(arm_ok=False)

        outcome = confirm(store, plan_id, line_id, last_price=D("130.00"), gateway=flaky)

        assert outcome.status == "BLOCKED"
        assert outcome.naked is True
        assert "NAKED" in outcome.reason
        assert f"position {position_id}" in outcome.reason
        position = store.positions[position_id]
        assert position["gtt_id"] is None
        assert position["gtt_trigger"] is None
        # The INTENT is recorded, so the next re-arm rests the stop at the right level.
        assert position["stop_price"] == D("104.00")

    def test_a_naked_line_is_what_the_sweep_then_finds(self) -> None:
        """The two halves and the sweep are one mechanism: ``gtt_id = None`` is the signal."""
        store, plan_id, line_id = a_store(kind="RAISE_GTT_STOP", stop_price=D("104.00"))
        a_position(store, gtt_id="551234", gtt_trigger="80.00")
        confirm(
            store, plan_id, line_id, last_price=D("130.00"), gateway=FlakyGateway(arm_ok=False)
        )
        assert [row["symbol"] for row in X.naked_positions(store)] == ["TWTCO"]

    def test_a_fill_whose_arm_fails_is_reported_naked_too(self) -> None:
        """The same rule on the entry side: shares held and no stop resting is one word."""
        store, plan_id, line_id = a_store()
        outcome = run(
            X.execute_line(
                store,
                _BuyOkArmFails(),
                plan_id=plan_id,
                line_id=line_id,
                confirm="true",
                now=NOW,
                last_price=D("100.00"),
            )
        )
        assert outcome.status == "BLOCKED"
        assert outcome.naked is True
        assert "NAKED" in outcome.reason
        assert store.positions[outcome.position_id]["gtt_id"] is None


class _BuyOkArmFails:
    """A gateway whose buy simulates and whose GTT will not arm. The fill-without-a-stop case."""

    async def place(self, **_: object) -> dict:
        return {"status": "DRY_RUN", "order_id": "DRY-1"}

    async def place_gtt_stop(self, **_: object) -> dict:
        return {"status": "GTT_ERROR", "error": "the exchange refused the trigger"}

    async def delete_gtt(self, **_: object) -> dict:
        raise AssertionError("a fill never cancels a trigger")


# =========================================================================================
# G8 (the module half) — the four chores: rearm, sweep, reconcile, halt
# =========================================================================================
class TestRearmAndSweep:
    def test_rearm_arms_a_naked_position(self) -> None:
        store = MemoryStore()
        position_id = a_position(store, gtt_id=None, gtt_trigger=None)
        outcome = run(
            X.rearm_gtt(
                store,
                FlakyGateway(),
                position_id=position_id,
                confirm="true",
                now=NOW,
                last_price=D("130.00"),
            )
        )
        assert outcome.status == "SIMULATED"
        assert store.positions[position_id]["gtt_id"] == "998877"

    def test_rearm_refuses_a_position_that_already_has_a_trigger(self) -> None:
        """Two triggers on one position sell twice what is held when they fire."""
        store = MemoryStore()
        position_id = a_position(store, gtt_id="551234")
        outcome = run(
            X.rearm_gtt(
                store, FlakyGateway(), position_id=position_id, confirm="true", now=NOW
            )
        )
        assert outcome.status == "BLOCKED"
        assert "already resting" in outcome.reason

    def test_rearm_without_a_confirm_is_a_four_hundred(self) -> None:
        store = MemoryStore()
        position_id = a_position(store, gtt_id=None, gtt_trigger=None)
        with pytest.raises(HTTPException) as caught:
            run(
                X.rearm_gtt(
                    store, FlakyGateway(), position_id=position_id, confirm="no", now=NOW
                )
            )
        assert caught.value.status_code == 400

    def test_rearm_refuses_a_stop_at_or_above_the_last_price(self) -> None:
        """A GTT that would fire on the next tick is not protection; it is a market order with
        extra steps, and the decision to sell belongs to a person."""
        store = MemoryStore()
        position_id = a_position(store, gtt_id=None, gtt_trigger=None, stop="80.00")
        outcome = run(
            X.rearm_gtt(
                store,
                FlakyGateway(),
                position_id=position_id,
                confirm="true",
                now=NOW,
                last_price=D("79.00"),
            )
        )
        assert outcome.status == "BLOCKED"
        assert "is not protection" in outcome.reason

    def test_the_sweep_rearms_what_is_naked_and_counts_what_is_left(self) -> None:
        store = MemoryStore()
        a_position(store, instrument_id=42, symbol="GOODCO", gtt_id=None, gtt_trigger=None)
        a_position(store, instrument_id=43, symbol="BADCO", gtt_id=None, gtt_trigger=None)
        report = run(
            X.sweep_naked(
                store,
                FlakyGateway(),
                now=NOW,
                prices={"GOODCO": D("130.00")},  # BADCO has no quote: armed against its entry
            )
        )
        assert report.naked_before == 2
        assert report.naked == 0
        assert store.sessions[NOW.date()]["naked"] == 0

    def test_the_sweep_is_idempotent_on_the_same_day(self) -> None:
        store = MemoryStore()
        a_position(store, gtt_id=None, gtt_trigger=None)
        first = run(X.sweep_naked(store, FlakyGateway(), now=NOW, prices={"TWTCO": D("130")}))
        second = run(X.sweep_naked(store, FlakyGateway(), now=NOW, prices={"TWTCO": D("130")}))
        assert first.rearmed == 1
        assert second.naked_before == 0 and second.rearmed == 0 and second.naked == 0

    def test_the_sweep_records_what_it_could_not_fix(self) -> None:
        store = MemoryStore()
        a_position(store, gtt_id=None, gtt_trigger=None)
        report = run(
            X.sweep_naked(
                store, FlakyGateway(arm_ok=False), now=NOW, prices={"TWTCO": D("130.00")}
            )
        )
        assert report.naked == 1
        assert store.sessions[NOW.date()]["naked"] == 1


class TestTheSeamTw7Injects:
    """``tools/twt/sweep.py``'s ``Rearm`` — one position in, one outcome out."""

    def test_the_callable_answers_the_shape_rearm_outcome_is_built_from(self) -> None:
        store = MemoryStore()
        position_id = a_position(store, gtt_id=None, gtt_trigger=None)
        rearm = X.rearm_callable(
            store, FlakyGateway(), now=NOW, price_for=lambda _symbol: D("130.00")
        )

        answer = run(rearm(position_id))

        assert set(answer) == {"position_id", "armed", "gtt_id", "reason"}
        assert answer["position_id"] == position_id
        assert answer["armed"] is True
        assert answer["gtt_id"] == "998877"
        assert answer["reason"] == ""

    def test_a_refusal_comes_back_armed_false_with_the_reason_to_read(self) -> None:
        """FIRST-LIVE-MORNING §9.2 step 2: the refusal is what tells a person what to do next."""
        store = MemoryStore()
        position_id = a_position(store, gtt_id=None, gtt_trigger=None, stop="80.00")
        rearm = X.rearm_callable(
            store, FlakyGateway(), now=NOW, price_for=lambda _symbol: D("79.00")
        )

        answer = run(rearm(position_id))

        assert answer["armed"] is False
        assert answer["gtt_id"] is None
        assert "is not protection" in answer["reason"]

    def test_the_callable_never_places_a_buy(self) -> None:
        store = MemoryStore()
        position_id = a_position(store, gtt_id=None, gtt_trigger=None)
        flaky = FlakyGateway()  # its `place` raises if anything asks it to buy
        run(X.rearm_callable(store, flaky, now=NOW, price_for=lambda _s: D("130.00"))(position_id))
        assert store.orders == {}


class TestReconcile:
    def test_reconcile_attaches_a_hand_armed_gtt_to_its_position(self) -> None:
        """Without it a line Maulik protects by hand reads naked forever."""
        store = MemoryStore()
        position_id = a_position(store, gtt_id=None, gtt_trigger=None)
        source = _Gtts([{"gtt_id": 771122, "symbol": "TWTCO", "trigger": 81.5, "status": "active"}])

        report = X.reconcile_gtts(store, source, now=NOW)

        assert report.attached == 1
        position = store.positions[position_id]
        assert position["gtt_id"] == "771122"
        assert position["gtt_trigger"] == D("81.5")
        assert X.naked_positions(store) == []

    def test_reconcile_leaves_a_trigger_it_cannot_match_alone_and_counts_it(self) -> None:
        """A second trigger in a name the sleeve already protects: **counted, never cancelled.**

        The safe direction for a discrepancy is always *more* protection — §9.3's one thing never
        to do is cancel a resting stop to tidy one up — so this leaves both where they are and
        puts the number in front of a person.
        """
        store = MemoryStore()
        a_position(store, gtt_id="551234")
        source = _Gtts([{"gtt_id": 771122, "symbol": "TWTCO", "trigger": 79.0}])
        report = X.reconcile_gtts(store, source, now=NOW)
        assert report.attached == 0
        assert report.unmatched == 1
        assert store.positions[next(iter(store.positions))]["gtt_id"] == "551234"

    def test_another_books_resting_gtt_is_not_a_discrepancy(self) -> None:
        """The account holds the weekly book's stops too, and this sleeve cannot see them."""
        store = MemoryStore()
        a_position(store, gtt_id="551234")
        source = _Gtts([{"gtt_id": 771122, "symbol": "SOMEONEELSECO", "trigger": 10.0}])
        report = X.reconcile_gtts(store, source, now=NOW)
        assert report.attached == 0
        assert report.unmatched == 0

    def test_reconcile_without_a_broker_changes_nothing(self) -> None:
        store = MemoryStore()
        a_position(store, gtt_id=None, gtt_trigger=None)
        assert X.reconcile_gtts(store, None, now=NOW).attached == 0
        assert len(X.naked_positions(store)) == 1


class _Gtts:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def list_gtts(self) -> list[dict]:
        return self.rows


class TestTheHalt:
    """FIRST-LIVE-MORNING §7's four behaviours. **The third is the one that matters.**"""

    def test_halt_zeroes_the_capital_and_audits_the_previous_value(self) -> None:
        store = MemoryStore()
        report = X.halt_sleeve(store, confirm="true", now=NOW)

        assert report.halted is True
        assert report.sleeve_capital_inr_before == TWENTY_FIVE_LAKH
        assert store.config["sleeve_capital_inr"] == D(0)
        audit = [row for row in store.audit if row["key"] == "sleeve_capital_inr"]
        assert len(audit) == 1
        assert audit[0]["old_value"] == "2500000.00"
        assert audit[0]["new_value"] == "0"

    def test_halt_expires_every_live_plan(self) -> None:
        store, plan_id, line_id = a_store()
        report = X.halt_sleeve(store, confirm="true", now=NOW)
        assert report.plans_expired == 1
        with pytest.raises(HTTPException) as caught:
            confirm(store, plan_id, line_id, last_price=D("100.00"))
        assert caught.value.status_code == 410

    def test_halt_never_touches_protection(self) -> None:
        """**The behaviour a test must pin.** A halt that removed stops would be the most
        dangerous button in the product.

        After the halt: every resting GTT is exactly where it was, the position still holds its
        shares, and a ``RAISE_GTT_STOP`` still plans and still arms. A halted sleeve cannot
        *buy*; everything it holds keeps its stop and keeps ratcheting.
        """
        store, plan_id, line_id = a_store(kind="RAISE_GTT_STOP", stop_price=D("104.00"))
        position_id = a_position(store, gtt_id="551234", gtt_trigger="80.00")
        before = dict(store.positions[position_id])

        X.halt_sleeve(store, confirm="true", now=NOW)

        after = store.positions[position_id]
        assert after["gtt_id"] == before["gtt_id"] == "551234"
        assert after["gtt_trigger"] == before["gtt_trigger"] == D("80.00")
        assert after["quantity_open"] == before["quantity_open"]
        assert after["state"] == "OPEN"

        # And the ratchet still works — on a plan built after the halt, since the halt expired
        # the one that was live. That is the point: protection is unaffected, buying is not.
        store.plans[plan_id]["expires_at"] = NOW + dt.timedelta(minutes=25)
        flaky = FlakyGateway()
        outcome = confirm(store, plan_id, line_id, last_price=D("130.00"), gateway=flaky)
        assert outcome.status == "SIMULATED"
        assert store.positions[position_id]["gtt_trigger"] == D("104.00")

    def test_a_halted_sleeve_cannot_buy(self) -> None:
        """At ₹0 every confirm of a ``BUY_AT_OPEN`` is BLOCKED, because the confirm re-sizes
        through the same rules the plan did (``04`` §10.5)."""
        store, plan_id, line_id = a_store()
        X.halt_sleeve(store, confirm="true", now=NOW)
        store.plans[plan_id]["expires_at"] = NOW + dt.timedelta(minutes=25)

        outcome = confirm(store, plan_id, line_id, last_price=D("100.00"))

        assert outcome.status == "BLOCKED"
        assert outcome.reason.startswith("NO_SLEEVE_CAPITAL")
        assert store.orders == {}

    def test_halt_says_what_it_did_in_one_line(self) -> None:
        store = MemoryStore()
        report = X.halt_sleeve(store, confirm="true", now=NOW)
        line = report.line()
        assert line.count("\n") == 0
        assert "₹2500000.00" in line and "₹0" in line
        assert "untouched" in line

    def test_halt_without_a_confirm_is_a_four_hundred(self) -> None:
        store = MemoryStore()
        with pytest.raises(HTTPException) as caught:
            X.halt_sleeve(store, confirm="", now=NOW)
        assert caught.value.status_code == 400
        assert store.config["sleeve_capital_inr"] == TWENTY_FIVE_LAKH


# =========================================================================================
# Fills that arrive later
# =========================================================================================
class TestOnOrderUpdate:
    def test_a_completed_buy_becomes_the_position_the_fill_and_the_gtt(self) -> None:
        store = MemoryStore()
        order_row = store.create_order(
            {
                "instrument_id": 42,
                "signal_date": SESSION,
                "side": "BUY",
                "quantity": 100,
                "stop_price": D("80.00"),
                "state": "SENT",
                "broker_order_id": "250911000001",
                "client_id": "x:TWTCO",
                "simulated": False,
            }
        )
        outcome = run(
            X.on_order_update(
                store,
                FlakyGateway(),
                {
                    "order_id": "250911000001",
                    "status": "COMPLETE",
                    "filled_quantity": 100,
                    "average_price": "101.00",
                },
                now=NOW,
            )
        )
        assert outcome is not None
        position = store.positions[outcome.position_id]
        assert position["quantity_open"] == 100
        assert position["entry_avg"] == D("101.00")
        assert position["initial_stop"] == D("80.80")
        assert position["gtt_id"] == "998877"
        assert store.orders[order_row]["state"] == "FILLED"

    def test_an_order_this_sleeve_does_not_own_is_ignored(self) -> None:
        store = MemoryStore()
        assert (
            run(
                X.on_order_update(
                    store, FlakyGateway(), {"order_id": "nope", "status": "COMPLETE"}, now=NOW
                )
            )
            is None
        )

    def test_an_open_order_is_not_a_fill(self) -> None:
        store = MemoryStore()
        store.create_order(
            {
                "instrument_id": 42,
                "signal_date": SESSION,
                "side": "BUY",
                "quantity": 100,
                "stop_price": D("80.00"),
                "state": "SENT",
                "broker_order_id": "1",
                "client_id": "x",
                "simulated": False,
            }
        )
        assert (
            run(
                X.on_order_update(
                    store, FlakyGateway(), {"order_id": "1", "status": "OPEN"}, now=NOW
                )
            )
            is None
        )


# =========================================================================================
# G7 — the spy: 0 orders reach a broker in the whole suite
# =========================================================================================
class TestNoOrderReachesABrokerInDryRun:
    def test_the_gates_are_cnc_only_and_dry_run_while_the_flag_is_false(self) -> None:
        """Non-negotiable 5 — intraday and options are false unconditionally."""
        gates = X.twt_gates()
        assert gates.dry_run is True
        assert gates.intraday_enabled is False
        assert gates.options_enabled is False

    def test_the_flag_is_false_by_default_and_no_auto_execute_setting_exists(self) -> None:
        """Non-negotiable 1 — the named exception is the swing sleeve's alone."""
        import app.config as config_module

        assert config_module.TWT_EXECUTION_ENABLED is False
        assert not [name for name in dir(config_module) if name.startswith("TWT_AUTO")]

    def test_no_broker_client_method_is_ever_invoked_by_a_confirm(self) -> None:
        """The complement of :class:`ExplodingKC`, said as an assertion rather than as a crash.

        ``ExplodingKC`` proves a broker call would have *failed*; this proves none was *made*.
        A confirm that fills and arms — the longest path this module has — touches the broker
        client not once while the flag is false, because every refusal happens in the gateway's
        dry-run branch above it.
        """
        seen: list[str] = []

        class _CountingKC(ExplodingKC):
            def __getattribute__(self, name: str) -> object:
                if name in {"place_order", "place_gtt", "delete_gtt", "cancel_order"}:
                    seen.append(name)
                return object.__getattribute__(self, name)

        store, plan_id, line_id = a_store()
        outcome = run(
            X.execute_line(
                store,
                X.build_twt_gateway(_CountingKC(), RiskManager()),
                plan_id=plan_id,
                line_id=line_id,
                confirm="true",
                now=NOW,
                last_price=D("100.00"),
            )
        )
        assert outcome.status == "SIMULATED"
        assert seen == []

    def test_the_buy_is_cnc_market_and_never_intraday(self) -> None:
        """Non-negotiable 5 and ``04`` §5.1: CNC, MARKET, at the next open."""
        seen: list[dict] = []

        class _Recorder:
            async def place(self, **kwargs: object) -> dict:
                seen.append(dict(kwargs))
                return {"status": "DRY_RUN", "order_id": "DRY-1"}

            async def place_gtt_stop(self, **_: object) -> dict:
                return {"status": "DRY_RUN_GTT", "gtt_id": None, "client_id": "c"}

        store, plan_id, line_id = a_store()
        confirm(store, plan_id, line_id, last_price=D("100.00"), gateway=_Recorder())
        assert seen[0]["product"] == "CNC"
        assert seen[0]["order_type"] == "MARKET"
        assert seen[0]["side"] == "BUY"

    def test_the_spy_saw_no_place_answer_anything_but_dry_run(self) -> None:
        """**The spy**, asserted from the gateway's own answers rather than by reading the code.

        It runs a real confirm of its own so the check does not depend on test order, then reads
        the shared tape — which by now also carries every call the tests above made, since
        :meth:`SpyGateway._record` asserts on each one as it happens.

        Two independent proofs of the same sentence: the status, here, and ``ExplodingKC``,
        which would have raised the moment anything reached Zerodha.
        """
        store, plan_id, line_id = a_store()
        outcome = confirm(store, plan_id, line_id, last_price=D("100.00"))
        assert outcome.simulated is True

        tape = list(SpyGateway.tape)
        assert ("place", "DRY_RUN") in tape
        assert [call for call, status in tape if not status.startswith("DRY_RUN")] == []
        assert [status for _call, status in tape if status in {"PLACED", "GTT_PLACED"}] == []
