"""FO11 — the desk's half of the FO safety proof (`docs/fno/06` FO11, `02` Tracks B and C).

* **The flag matrix, at the gateway** (`TestTheFlagMatrixAtTheGateway`, no database): for each
  sleeve (F1N, F1B, F2), all 16 combinations of `DRY_RUN`, `OPTIONS_ENABLED`,
  `BASKFY_FNO_CARRY_ENABLED` and `BASKFY_FNO_<F1|F2>_EXECUTION_ENABLED`, each under both values of
  `INTRADAY_ENABLED`: a leg sent through the desk's FO gateway (`fno_execute.build_fo_gateway`,
  paper-pinned by FO7.1) never reaches a spy broker, in any row; a gateway built from
  `fno_gates.product_gates` alone reaches it in the one LIVE row only (the control that shows the
  pin and `LIVE_NOT_BUILT` are what stand there), and a half-flipped sleeve is refused by name.
* **`INTRADAY_ENABLED` never admits an FO order** (`TestIntradayIsNotAnFoFlag`): MIS on NFO with an
  `fo_plan`, NRML without one, CNC with one — refused before the broker with every switch on.
* **The flag matrix, through the real desk path** (`TestTheFlagMatrixThroughTheDesk`, Postgres):
  per sleeve and per row, a plan raised by the monitor, `POST /fno/execute` through the real route,
  executor and FO gateway, then the carry (a night's mark, F2's trail) and the exit (F1's E-1
  15:00; F2's roll and stop) under that row's switches. The LIVE row is `409 LIVE_NOT_BUILT`
  (FO7.1); a half-flipped row is refused leg by leg by the gateway; every other row is simulated
  end to end. The recording broker sees 0 calls and no gateway journal records a broker event.
* **A Hypothesis fuzz over `POST /fno/execute`** (`TestTheExecuteRouteFuzz`, Postgres) with every FO
  flag false and `DRY_RUN` either way: plan ids (own, foreign, an exit's, a roll's, garbage),
  confirm values, clocks across the 30-minute window, and tampered product/venue/quantity/mode
  fields. No NRML order is formed toward a broker (0 calls; every journalled order is the dry-run
  branch's), the answer is 200 exactly when the spec says a confirm is valid, and a tampered field
  changes nothing about what the gateway was asked (NRML on NFO, the plan's own legs).

The pure properties (never naked over fill scripts, exact exits, no position past E-1 15:00) and
the source scans are `decile-blueprint/packages/core/tests/test_fno_safety_proof.py`; the drill is
`tools/fno/drill.py`.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import itertools
import json
import sys
import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app import config as C
from app import fno_execute as X
from app import fno_monitor as M
from app.core.gateway import OrderGateway
from app.fno_gates import fno_gates, product_gates
from baskfy_core.fno.config import FoSleeve, FoSleeveGroup, group_of
from baskfy_core.fno.covered import OptionPosition, uncovered
from baskfy_core.options.config import Mode, OptionType
from baskfy_execution.guards import FoPlanRef
from baskfy_execution.risk import RiskManager
from test_fno_desk import Desk
from test_fno_monitor import (  # noqa: F401 - the FO7 replay harness and its fixtures
    DEC,
    F1_ENTRY,
    F2_ENTRY,
    FUT_DEC,
    FUT_NOV,
    HARD_EXIT,
    LOT,
    NEUTRAL,
    NOV,
    STRIKES,
    TYPES,
    FakeInstruments,
    World,
    at,
    before,
    book,
    conn,
    f2_scan,
    requires_db,
)
from test_vbt_safety import SpyingKC

SLEEVES = (FoSleeve.F1N, FoSleeve.F1B, FoSleeve.F2)
UNDERLYING = {FoSleeve.F1N: "NIFTY", FoSleeve.F1B: "BANKNIFTY", FoSleeve.F2: "RELIANCE"}
EXECUTION = {FoSleeveGroup.F1: "FNO_F1_EXECUTION_ENABLED",
             FoSleeveGroup.F2: "FNO_F2_EXECUTION_ENABLED"}  # fmt: skip
ROWS = list(itertools.product((False, True), repeat=4))  # dry_off, options, carry, execution
#: Journal events that mean the broker was called (or answered). None may appear.
BROKER_EVENTS = frozenset({
    "placed", "rejected", "error", "gtt_placed", "gtt_modified", "gtt_deleted", "gtt_error",
    "gtt_modify_error", "gtt_delete_error", "order_cancelled", "order_cancel_error",
})  # fmt: skip


def set_flags(  # noqa: PLR0913 - the four switches and INTRADAY_ENABLED, by keyword
    monkeypatch: pytest.MonkeyPatch, sleeve: FoSleeve, *, dry_off: bool, options: bool,
    carry: bool, execution: bool, intraday: bool,
) -> bool:  # fmt: skip
    """The four switches for `sleeve` (the other sleeve group's flag off); True when LIVE."""
    monkeypatch.setattr(C, "DRY_RUN", not dry_off)
    monkeypatch.setattr(C, "OPTIONS_ENABLED", options)
    monkeypatch.setattr(C, "FNO_CARRY_ENABLED", carry)
    monkeypatch.setattr(C, "INTRADAY_ENABLED", intraday)
    for flag in EXECUTION.values():
        monkeypatch.setattr(C, flag, False)
    monkeypatch.setattr(C, EXECUTION[group_of(sleeve)], execution)
    return dry_off and options and carry and execution


def all_off(monkeypatch: pytest.MonkeyPatch, *, dry_run: bool = True,
            intraday: bool = False) -> None:  # fmt: skip
    set_flags(monkeypatch, FoSleeve.F1N, dry_off=not dry_run, options=False, carry=False,
              execution=False, intraday=intraday)  # fmt: skip


# --- one FO leg, at the gateway -------------------------------------------------------------------


def opt_symbol(underlying: str, role: str) -> str:
    return f"{underlying}26NOV{STRIKES[role]}{TYPES[role]}"


def fo_leg(sleeve: FoSleeve, *, product: str = "NRML", with_ref: bool = True) -> dict[str, Any]:
    """The first leg a confirm would send: F1's long put (always covered), F2's future."""
    plan_id = f"{sleeve.value}-20261103-{UNDERLYING[sleeve]}-u1"
    if group_of(sleeve) is FoSleeveGroup.F2:
        symbol, ref = FUT_NOV, FoPlanRef(plan_id=plan_id, sleeve=sleeve.value)
    else:
        underlying = UNDERLYING[sleeve]
        symbol = opt_symbol(underlying, "LONG_PUT")
        step = OptionPosition(underlying, NOV, OptionType.PE, Decimal(STRIKES["LONG_PUT"]), LOT)
        ref = FoPlanRef(plan_id=plan_id, sleeve=sleeve.value, step=step)
    qty = 500 if group_of(sleeve) is FoSleeveGroup.F2 else LOT
    return {"symbol": symbol, "qty": qty, "side": "BUY", "product": product, "exchange": "NFO",
            "order_type": "LIMIT", "price": 10.0, "client_id": f"{plan_id}:{symbol}:{product}",
            "gross_exposure": 10.0 * qty, "reference_price": 10.0,
            **({"fo_plan": ref} if with_ref else {})}  # fmt: skip


def send(gateway: Any, order: dict[str, Any]) -> dict[str, Any]:  # noqa: ANN401 - a gateway
    async def go() -> dict[str, Any]:
        out: dict[str, Any] = await gateway.place(**order)
        return out

    return asyncio.run(go())


def pinned(sleeve: FoSleeve, spy: SpyingKC, tmp: Path) -> Any:  # noqa: ANN401
    """The desk's FO gateway, exactly as the route and the monitor build it."""
    path = tmp / f"pinned-{sleeve.value}-{uuid.uuid4().hex[:8]}.jsonl"  # a fresh journal per row
    return X.build_fo_gateway(sleeve, spy, RiskManager(), str(path))


def unpinned(sleeve: FoSleeve, spy: SpyingKC, tmp: Path) -> Any:  # noqa: ANN401
    """The same gateway on `fno_gates.product_gates` alone, without FO7.1's paper pin."""
    return OrderGateway(spy, RiskManager(), gates=lambda: product_gates(sleeve),
                        journal_path=str(tmp / f"bare-{uuid.uuid4().hex[:8]}.jsonl"),
                        coverage=uncovered)  # fmt: skip


class TestTheFlagMatrixAtTheGateway:
    @pytest.mark.parametrize("sleeve", SLEEVES)
    def test_sixteen_rows_twice_and_the_fo_gateway_never_reaches_the_broker(
        self, sleeve: FoSleeve, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for (dry_off, options, carry, execution), intraday in itertools.product(
            ROWS, (False, True)
        ):
            row = (sleeve, dry_off, options, carry, execution, intraday)
            live = set_flags(monkeypatch, sleeve, dry_off=dry_off, options=options, carry=carry,
                             execution=execution, intraday=intraday)  # fmt: skip
            assert (fno_gates(sleeve).mode is Mode.LIVE) is live, row
            order = fo_leg(sleeve)
            spy = SpyingKC()
            out = send(pinned(sleeve, spy, tmp_path), order)
            assert spy.calls == [], row  # FO7.1: the desk's FO gateway is paper in every row
            half_flip = execution and not (options and carry)
            if half_flip:
                assert out.get("status") not in ("DRY_RUN", "PLACED"), row
            else:
                assert out.get("status") == "DRY_RUN", (row, out)
            bare = SpyingKC()
            out = send(unpinned(sleeve, bare, tmp_path), order)
            if live:
                # The control: with all four on, only FO7.1's pin (and execute_entry's
                # LIVE_NOT_BUILT) stands between a confirm and the broker.
                assert bare.calls == ["place_order"], (row, out)
            else:
                assert bare.calls == [], (row, out)
            if half_flip:
                why = str(out.get("error", ""))
                assert "OPTIONS_ENABLED" in why or "BASKFY_FNO_CARRY_ENABLED" in why, (row, out)


class TestIntradayIsNotAnFoFlag:
    @pytest.fixture(autouse=True)
    def everything_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for flag in ("OPTIONS_ENABLED", "FNO_CARRY_ENABLED", "INTRADAY_ENABLED",
                     *EXECUTION.values()):  # fmt: skip
            monkeypatch.setattr(C, flag, True)
        monkeypatch.setattr(C, "DRY_RUN", False)

    @pytest.mark.parametrize("sleeve", SLEEVES)
    def test_product_gates_never_carry_intraday(self, sleeve: FoSleeve) -> None:
        assert fno_gates(sleeve).mode is Mode.LIVE
        assert product_gates(sleeve).intraday_enabled is False
        assert X.paper_gates(sleeve).intraday_enabled is False

    @pytest.mark.parametrize("sleeve", SLEEVES)
    @pytest.mark.parametrize(("product", "with_ref"), [("MIS", True), ("NRML", False),
                                                       ("CNC", True), ("MIS", False)])
    def test_only_nrml_with_a_plan_is_an_fo_order(
        self, sleeve: FoSleeve, product: str, with_ref: bool, tmp_path: Path
    ) -> None:
        for build in (pinned, unpinned):
            spy = SpyingKC()
            out = send(build(sleeve, spy, tmp_path), fo_leg(sleeve, product=product,
                                                           with_ref=with_ref))  # fmt: skip
            assert out.get("status") not in ("DRY_RUN", "PLACED"), (build.__name__, out)
            assert spy.calls == [], build.__name__

    def test_the_weekly_desk_gateway_refuses_an_fo_leg_whatever_intraday_says(
        self, tmp_path: Path
    ) -> None:
        """`_gates_from_config` never reads the carry flag (FO6.5): an NRML fo_plan leg sent
        through any other desk gateway is refused even with every switch on."""
        spy = SpyingKC()
        gw = OrderGateway(spy, RiskManager(), journal_path=str(tmp_path / "weekly.jsonl"))
        out = send(gw, fo_leg(FoSleeve.F2))
        assert out.get("status") not in ("DRY_RUN", "PLACED") and spy.calls == []


# --- the real desk path ---------------------------------------------------------------------------


class BothIndices(FakeInstruments):
    """The FO7 fixture master, with BANKNIFTY listed on NIFTY's strikes (F1B)."""

    def option(self, underlying: str, expiry: dt.date, strike: Decimal,
               option_type: str) -> M.Contract | None:  # fmt: skip
        if underlying not in ("NIFTY", "BANKNIFTY") or expiry != NOV:
            return None
        role = next((r for r, k in STRIKES.items() if k == strike and TYPES[r] == option_type),
                    None)  # fmt: skip
        if role is None:
            return None
        base = 7_000_000 if underlying == "NIFTY" else 7_500_000
        return M.Contract(opt_symbol(underlying, role), base + int(strike), LOT, NOV)


def condor_books(underlying: str) -> dict[str, X.FoQuote]:
    return {underlying + symbol.removeprefix("NIFTY"): q for symbol, q in NEUTRAL.items()}


def new_user(db: Any) -> int:  # noqa: ANN401 - the desk's Connection
    tag = uuid.uuid4().hex[:8]
    uid = int(db.execute(
        "INSERT INTO app_user (public_id, email) VALUES (?, ?) RETURNING id",
        (f"fo11-{tag}", f"fo11-{tag}@x.test"),
    ).fetchone()["id"])  # fmt: skip
    db.execute(
        "INSERT INTO fo_sleeve_config (user_id, sleeve, capital_inr) VALUES (?, 'F1', 2500000), "
        "(?, 'F2', 0)",
        (uid, uid),
    )
    return uid


@contextlib.contextmanager
def fresh_world(db: Any, tmp: Path) -> Iterator[World]:  # noqa: ANN401
    """A throwaway user and its replay world, deleted with everything it owns afterwards."""
    uid = new_user(db)
    where = tmp / f"u{uid}"
    where.mkdir(parents=True, exist_ok=True)
    try:
        w = World(db, uid, where)
        w.monitor.instruments = BothIndices()
        yield w
    finally:
        db.execute("DELETE FROM app_user WHERE id = ?", (uid,))


def raise_plan(w: World, sleeve: FoSleeve) -> tuple[str, dt.datetime]:
    """The monitor's 09:20 plan for `sleeve`, raised on paper; returns it and its issue time."""
    if group_of(sleeve) is FoSleeveGroup.F2:
        f2_scan(w)
        w.quotes.books[FUT_NOV] = book("1399.90", "1400.00", last="1400.00")
        issued = at(F2_ENTRY, 9, 20)
    else:
        underlying = UNDERLYING[sleeve]
        legs = [{"entry_seq": i, "role": r, "strike": str(STRIKES[r]), "option_type": TYPES[r],
                 "expiry": NOV.isoformat(), "qty_sign": 1 if r.startswith("LONG") else -1}
                for i, r in enumerate(("LONG_PUT", "LONG_CALL", "SHORT_PUT", "SHORT_CALL"), 1)]
        w.scan(sleeve.value, before(F1_ENTRY, 1), underlying, {
            "underlying": underlying, "entry_session": F1_ENTRY.isoformat(),
            "expiry": NOV.isoformat(), "hard_exit_date": HARD_EXIT.isoformat(), "legs": legs,
            "lot_size": LOT,
        })  # fmt: skip
        w.quotes.books.update(condor_books(underlying))
        issued = at(F1_ENTRY, 9, 20)
    raised = w.tick(issued).raised
    assert len(raised) == 1, raised
    assert w.plan_row(raised[0])["status"] == "ISSUED", w.plan_row(raised[0])
    return raised[0], issued


def journal_events(w: World) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in sorted(w.tmp.glob("*.jsonl")):
        out += [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return out


def assert_no_broker(w: World) -> list[dict[str, Any]]:
    assert w.kc.calls == []
    events = journal_events(w)
    assert not [e for e in events if e.get("event") in BROKER_EVENTS], events
    for e in events:
        if e.get("event") == "dry_run" and e.get("fo_plan"):
            assert e.get("product") == "NRML", e
    return events


def carry_and_exit(w: World, sleeve: FoSleeve, plan_id: str, position_id: int) -> str:
    """The carry and the exits under the confirm; returns the position's closing reason."""
    if group_of(sleeve) is FoSleeveGroup.F2:
        w.market.land_future(F2_ENTRY, NOV, close="1450", settle="1449")
        night = w.night(F2_ENTRY)
        assert night.marked == 1 and night.trailed == [
            "RELIANCE:1340.00->1390.00:DRY_RUN_GTT_MODIFY"], night
        w.quotes.books[FUT_NOV] = book("1430.00", "1430.10", last="1430.05")
        w.quotes.books[FUT_DEC] = book("1438.00", "1438.10", last="1438.05")
        assert w.tick(at(HARD_EXIT, 15, 0)).actions == [f"{plan_id}-R1"]
        assert w.position(position_id)["closed_at"] is None  # rolled, not closed
        day = before(DEC, 10)
        w.quotes.books[FUT_DEC] = book("1380.00", "1380.10", last="1380.00")
        assert w.tick(at(day, 10, 0)).actions == [f"{plan_id}-X"]
    else:
        underlying = UNDERLYING[sleeve]
        w.market.landed_days.add(F1_ENTRY)
        w.market.day_prints.setdefault(F1_ENTRY, {}).update({
            (underlying, NOV, Decimal(STRIKES[r]), TYPES[r]): M.Print(Decimal(s), Decimal(s))
            for r, s in (("SHORT_CALL", "58"), ("SHORT_PUT", "54"), ("LONG_CALL", "12"),
                         ("LONG_PUT", "10"))
        })  # fmt: skip
        assert w.night(F1_ENTRY).marked == 1
        assert w.tick(at(HARD_EXIT, 14, 59)).actions == []
        assert w.tick(at(HARD_EXIT, 15, 0)).actions == [f"{plan_id}-X"]
    pos = w.position(position_id)
    assert pos["closed_at"] is not None
    return str(pos["closed_reason"])


@requires_db
class TestTheFlagMatrixThroughTheDesk:
    @pytest.mark.parametrize(("dry_off", "options", "carry", "execution"), ROWS)
    @pytest.mark.parametrize("sleeve", SLEEVES)
    def test_every_row_of_the_route_reaches_no_broker(  # noqa: PLR0913, PLR0917 - the row
        self, conn: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,  # noqa: ANN401, F811
        sleeve: FoSleeve, dry_off: bool, options: bool, carry: bool, execution: bool,
    ) -> None:  # fmt: skip
        all_off(monkeypatch)
        with fresh_world(conn, tmp_path) as w:
            plan_id, issued = raise_plan(w, sleeve)
            live = set_flags(monkeypatch, sleeve, dry_off=dry_off, options=options, carry=carry,
                             execution=execution, intraday=True)  # fmt: skip
            desk = Desk(w, monkeypatch)
            desk.now = issued + dt.timedelta(minutes=2)
            r = desk.post(plan_id)
            body = r.json()
            if live:
                assert (r.status_code, body["code"]) == (409, "LIVE_NOT_BUILT")
                assert w.fills(plan_id) == [] and w.plan_row(plan_id)["status"] == "ISSUED"
            elif execution and not (options and carry):
                # A half-flipped sleeve: the gateway itself refuses each leg by name.
                assert r.status_code == 200 and body["outcome"] == "ABANDONED_PARTIAL", body
                assert "OPTIONS_ENABLED" in body["detail"] or "CARRY" in body["detail"], body
                assert w.fills(plan_id) == []
            else:
                assert r.status_code == 200 and body["outcome"] == "OPEN", body
                assert body["simulated"] is True
                fills = w.fills(plan_id)
                assert fills and {f["simulated"] for f in fills} == {True}
                assert {f["client_id"].split(":")[0] for f in fills} == {plan_id}
                reason = carry_and_exit(w, sleeve, plan_id, int(body["position_id"]))
                assert reason == ("STOP" if sleeve is FoSleeve.F2 else "HARD_EXIT")
                every = w.rows("SELECT bool_and(simulated) AS s FROM fo_fill WHERE user_id = ?",
                               w.uid)  # fmt: skip
                assert every[0]["s"] is True
            assert_no_broker(w)


# --- the fuzz -------------------------------------------------------------------------------------

CONFIRMS = st.one_of(
    st.just("true"),
    st.just("true"),
    st.sampled_from(["True", "TRUE", "1", "yes", "on", "", "false", " true", "true ", "t",
                     "null", "true\x00"]),
    st.text(max_size=6),
)  # fmt: skip
TAMPER_FIELDS = ("product", "exchange", "qty", "quantity", "side", "tradingsymbol", "sleeve",
                 "mode", "dry_run", "order_type", "price", "variety", "fo_plan", "structure")
TAMPER_VALUES = ("MIS", "CNC", "NRML", "BO", "CO", "NSE", "BFO", "MCX", "CDS", "NFO", "999999",
                 "-1", "SELL", "BUY", "SGBMAR31", "LIVE", "false", "0", "MARKET", "F2", "F1N",
                 "NAKED_SHORT", "AMO")  # fmt: skip
PLAN_CHOICES = ("own", "own", "own", "foreign", "exit", "roll", "garbage", "prefix")


@requires_db
class TestTheExecuteRouteFuzz:
    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture,
                                     HealthCheck.too_slow])  # fmt: skip
    @given(
        sleeve=st.sampled_from(SLEEVES),
        dry_run=st.booleans(),
        intraday=st.booleans(),
        choice=st.sampled_from(PLAN_CHOICES),
        garbage=st.text(max_size=40),
        confirm=CONFIRMS,
        minutes=st.one_of(st.integers(0, 29), st.integers(0, 120)),
        tamper=st.dictionaries(st.sampled_from(TAMPER_FIELDS), st.sampled_from(TAMPER_VALUES),
                               max_size=5),
        twice=st.booleans(),
    )
    def test_no_input_forms_an_order_toward_the_broker(  # noqa: PLR0913, PLR0917 - the request
        self, conn: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,  # noqa: ANN401, F811
        sleeve: FoSleeve, dry_run: bool, intraday: bool, choice: str, garbage: str, confirm: str,
        minutes: int, tamper: dict[str, str], twice: bool,
    ) -> None:  # fmt: skip
        all_off(monkeypatch, dry_run=dry_run, intraday=intraday)
        with fresh_world(conn, tmp_path) as other, fresh_world(conn, tmp_path) as w:
            foreign_id, _ = raise_plan(other, sleeve)
            own_id, issued = raise_plan(w, sleeve)
            plan_id = {
                "own": own_id, "foreign": foreign_id, "exit": f"{own_id}-X",
                "roll": f"{own_id}-R1", "garbage": garbage, "prefix": own_id[:-1],
            }[choice]  # fmt: skip
            desk = Desk(w, monkeypatch)
            desk.now = issued + dt.timedelta(minutes=minutes)
            expires = X._aware(w.plan_row(own_id)["expires_at"])
            data = {**tamper, "plan_id": plan_id, "confirm": confirm}
            answers = [desk.client.post("/fno/execute", data=data)
                       for _ in range(2 if twice else 1)]  # fmt: skip
            valid = confirm == "true" and plan_id == own_id and desk.now < expires
            first = answers[0]
            event(f"{first.status_code} {'valid' if valid else 'refused'}")
            assert first.status_code in (200, 400, 404, 409, 410), first.text
            assert (first.status_code == 200) is valid, (first.status_code, first.text)
            if valid:
                assert first.json()["outcome"] == "OPEN" and first.json()["simulated"] is True
                fills = w.fills(own_id)
                assert fills and {f["simulated"] for f in fills} == {True}
                symbols = {lg["tradingsymbol"] for lg in w.rows(
                    "SELECT l.tradingsymbol FROM fo_leg l JOIN fo_plan p ON p.id = l.plan_id "
                    "WHERE p.plan_id = ?", own_id)}  # fmt: skip
                orders = [e for e in journal_events(w) if e.get("event") == "dry_run"]
                assert orders and {e["symbol"] for e in orders} <= symbols
                assert {e["product"] for e in orders} == {"NRML"}  # the tamper moved nothing
            else:
                assert w.fills(own_id) == []
            if twice and valid:
                assert (answers[1].status_code, answers[1].json()["code"]) == (409, "NOT_ISSUED")
            assert other.fills(foreign_id) == [] and other.plan_row(foreign_id)["status"] == (
                "ISSUED")
            assert_no_broker(w)
            assert_no_broker(other)
