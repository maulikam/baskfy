"""FO7 — the desk's FO monitor, replayed over a recorded fixture month (``docs/fno/06`` FO7).

Goal: plans are raised, exits happen, and positions carry, on paper. Each replay drives the real
engine (`app.fno_monitor.FnoMonitor`), the real executor (`app.fno_execute`) and the real FO
gateway (paper-pinned, the covered-overnight rule wired) over a real PostgreSQL with the `fo_`
schema, with a fake clock, a fixture book of Kite quotes and depth, a fixture F&O bhavcopy and a
recording broker that must see **nothing**:

* F1 (`TestF1Replay`): the 09:20 plan from `fo_scan` (legs longs first, `expires_at` 09:50),
  confirm → four simulated fills and `OPEN`; carried across nights with a `fo_mark` at each
  settle (a session whose bhavcopy has not landed is marked later, idempotently); the profit take,
  the loss close and the 15:00 `E - 1` exit, each an EXIT plan under the entry's confirm closed
  shorts first; a hard-exit date passed while the desk was down → `LATE_EXIT` at the next open.
* F2 (`TestF2Replay`): the 09:20 plan (one lot on paper, `lots_at_ceiling` recorded); the fill and
  its GTT in the same session; the evening trail moving the GTT through the gateway and never
  lowering it; the `E - 1` roll into the next month with the stop carried; the 40-session time
  exit; a stop-out; a GTT that fails to place → alert, `NAKED_FUTURE`, exit at the next check.
* Gating (`TestGating`): the flag false → nothing is built; no path forms an entry without a
  confirm; with every FO flag false every order is simulated and the broker sees 0 calls (and the
  paper pin holds with every flag true).

Skipped without `BASKFY_TEST_DATABASE_URL`.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import inspect
import json
import os
import re
import uuid
from collections.abc import Iterator, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from app import config as C
from app import fno_execute as X
from app import fno_monitor as M
from baskfy_core.fno.calendar import sessions_before
from baskfy_core.fno.config import FoSleeve
from baskfy_core.options.chain import Level
from baskfy_execution.risk import RiskManager

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SESSIONS = [
    dt.date(2026, 10, 1) + dt.timedelta(days=i)
    for i in range(130)
    if (dt.date(2026, 10, 1) + dt.timedelta(days=i)).weekday() < 5
]


def before(anchor: dt.date, n: int) -> dt.date:
    found = sessions_before(SESSIONS, anchor, n)
    assert found is not None
    return found


NOV = dt.date(2026, 11, 24)  # the November monthly (last Tuesday)
DEC = dt.date(2026, 12, 29)
F1_ENTRY = before(NOV, 15)  # 3 Nov
HARD_EXIT = before(NOV, 1)  # 23 Nov
LOT = 65
STRIKES = {"LONG_CALL": 25400, "SHORT_CALL": 25000, "SHORT_PUT": 23000, "LONG_PUT": 22600}
TYPES = {"LONG_CALL": "CE", "SHORT_CALL": "CE", "SHORT_PUT": "PE", "LONG_PUT": "PE"}
F2_ENTRY = dt.date(2026, 11, 2)
F2_LOT = 500
FUT_NOV, FUT_DEC = "RELIANCE26NOVFUT", "RELIANCE26DECFUT"


def at(day: dt.date, hh: int, mm: int = 0) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hh, mm), tzinfo=IST)


def opt_symbol(role: str) -> str:
    return f"NIFTY26NOV{STRIKES[role]}{TYPES[role]}"


# --- the fixture world ---------------------------------------------------------------------------


def book(bid: str, ask: str, *, last: str | None = None, depth: int = 100000) -> X.FoQuote:
    b, a = Decimal(bid), Decimal(ask)
    return X.FoQuote(b, a, (Level(b, depth),), (Level(a, depth),),
                     Decimal(last) if last else (b + a) / 2, 1000000)  # fmt: skip


def condor_book(sc: str, sp: str, lc: str, lp: str) -> dict[str, X.FoQuote]:
    """Each leg a one-rupee-wide book around the given mid (shorts) or a 0.5 book (wings)."""
    out = {}
    for role, mid, half in (("SHORT_CALL", sc, "0.5"), ("SHORT_PUT", sp, "0.5"),
                            ("LONG_CALL", lc, "0.25"), ("LONG_PUT", lp, "0.25")):  # fmt: skip
        m, h = Decimal(mid), Decimal(half)
        out[opt_symbol(role)] = book(str(m - h), str(m + h))
    return out


NEUTRAL = condor_book("60.5", "55.5", "12.25", "10.25")  # credit on mids 93.50


class FakeQuotes:
    def __init__(self) -> None:
        self.books: dict[str, X.FoQuote] = {}

    def __call__(self, symbols: Sequence[str]) -> Mapping[str, X.FoQuote]:
        return {s: self.books[s] for s in symbols if s in self.books}


class FakeInstruments:
    def option(self, underlying: str, expiry: dt.date, strike: Decimal,
               option_type: str) -> M.Contract | None:  # fmt: skip
        if underlying != "NIFTY" or expiry != NOV:
            return None
        role = next((r for r, k in STRIKES.items() if k == strike and TYPES[r] == option_type),
                    None)  # fmt: skip
        if role is None:
            return None
        return M.Contract(opt_symbol(role), 7_000_000 + int(strike), LOT, NOV)

    def future(self, symbol: str, expiry: dt.date) -> M.Contract | None:
        if symbol != "RELIANCE":
            return None
        return {NOV: M.Contract(FUT_NOV, 8_000_001, F2_LOT, NOV),
                DEC: M.Contract(FUT_DEC, 8_000_002, F2_LOT, DEC)}.get(expiry)  # fmt: skip

    def next_future(self, symbol: str, after: dt.date) -> M.Contract | None:
        return self.future(symbol, DEC) if after < DEC else None


class FakeMarket:
    def __init__(self) -> None:
        self.landed_days: set[dt.date] = set()
        self.day_prints: dict[dt.date, dict[M.PrintKey, M.Print]] = {}

    def sessions(self) -> Sequence[dt.date]:
        return SESSIONS

    def landed(self, day: dt.date) -> bool:
        return day in self.landed_days

    def prints(self, day: dt.date, keys: Sequence[M.PrintKey]) -> Mapping[M.PrintKey, M.Print]:
        found = self.day_prints.get(day, {})
        return {k: found[k] for k in keys if k in found}

    def land_condor(self, day: dt.date, settles: Mapping[str, str]) -> None:
        self.landed_days.add(day)
        self.day_prints.setdefault(day, {}).update({
            ("NIFTY", NOV, Decimal(STRIKES[r]), TYPES[r]): M.Print(Decimal(s), Decimal(s))
            for r, s in settles.items()
        })  # fmt: skip

    def land_future(self, day: dt.date, expiry: dt.date, close: str, settle: str) -> None:
        self.landed_days.add(day)
        self.day_prints.setdefault(day, {})[("RELIANCE", expiry, None, "XX")] = M.Print(
            Decimal(close), Decimal(settle))


class RecordingKC:
    """Any broker method at all is recorded: the assertion is that the list stays empty."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 - any broker method
        if name.startswith("__"):
            raise AttributeError(name)

        def record(*_a: object, **_k: object) -> None:
            self.calls.append(name)

        return record


# --- the database --------------------------------------------------------------------------------


def _pg_url() -> str | None:
    url = os.environ.get("BASKFY_TEST_DATABASE_URL")
    return None if not url else url.replace("postgresql+asyncpg://", "postgresql://")


requires_db = pytest.mark.skipif(_pg_url() is None, reason="BASKFY_TEST_DATABASE_URL is not set")


@pytest.fixture
def conn() -> Iterator[Any]:
    url = _pg_url()
    if url is None:
        pytest.skip("BASKFY_TEST_DATABASE_URL is not set")
    from app.analytics.pg import Connection  # noqa: PLC0415 - psycopg only with a database

    c = Connection(url)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def user(conn: Any) -> Iterator[int]:  # noqa: ANN401 - the desk's Connection
    tag = uuid.uuid4().hex[:8]
    uid = int(conn.execute(
        "INSERT INTO app_user (public_id, email) VALUES (?, ?) RETURNING id",
        (f"fo7-{tag}", f"fo7-{tag}@x.test"),
    ).fetchone()["id"])  # fmt: skip
    conn.execute(
        "INSERT INTO fo_sleeve_config (user_id, sleeve, capital_inr) VALUES (?, 'F1', 2500000), "
        "(?, 'F2', 0)",
        (uid, uid),
    )
    try:
        yield uid
    finally:
        conn.execute("DELETE FROM app_user WHERE id = ?", (uid,))


class World:
    def __init__(self, conn: Any, uid: int, tmp: Path) -> None:  # noqa: ANN401
        self.conn = conn
        self.uid = uid
        self.store = X.FoStore(conn, user_id=uid)
        self.quotes = FakeQuotes()
        self.market = FakeMarket()
        self.kc = RecordingKC()
        self.alerts: list[str] = []
        self.margin: M.MarginQuote | None = M.MarginQuote(Decimal(150000), Decimal(1000000))
        self.gateways: dict[FoSleeve, Any] = {}
        self.tmp = tmp
        self.monitor = M.FnoMonitor(
            store=self.store, gateway_for=self.gateway_for, quotes=self.quotes,
            instruments=FakeInstruments(), margins=lambda _legs: self.margin,
            market=self.market, alert=self.alerts.append,
        )  # fmt: skip

    def gateway_for(self, sleeve: FoSleeve) -> Any:  # noqa: ANN401
        if sleeve not in self.gateways:
            self.gateways[sleeve] = X.build_fo_gateway(
                sleeve, self.kc, RiskManager(), str(self.tmp / f"fo-{sleeve.value}.jsonl"))
        return self.gateways[sleeve]

    def tick(self, now: dt.datetime) -> M.TickReport:
        return asyncio.run(self.monitor.tick(now))

    def night(self, day: dt.date) -> M.NightReport:
        return asyncio.run(self.monitor.nightly(day))

    def confirm(self, plan_id: str, now: dt.datetime, *, confirm: bool = True) -> X.FoOutcome:
        async def go() -> X.FoOutcome:
            return await X.execute_entry(
                self.store, self.gateway_for(FoSleeve(plan_id.split("-", maxsplit=1)[0])),
                quotes=self.quotes, plan_id=plan_id, confirm=confirm, now=lambda: now,
                alert=self.alerts.append,
            )  # fmt: skip

        return asyncio.run(go())

    def scan(self, sleeve: str, trade_date: dt.date, symbol: str, detail: dict[str, Any],
             rv20: str | None = None) -> None:  # fmt: skip
        self.conn.execute(
            "INSERT INTO fo_scan (user_id, sleeve, trade_date, symbol, state, reasons, detail, "
            "rv20) VALUES (?, ?, ?, ?, 'CANDIDATE', ?, ?, ?)",
            (self.uid, sleeve, trade_date, symbol, ["candidate"], json.dumps(detail), rv20),
        )

    def rows(self, sql: str, *params: object) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def fills(self, plan_id: str) -> list[dict[str, Any]]:
        return self.rows(
            "SELECT l.role, f.side, f.quantity, f.price, f.simulated, f.client_id FROM fo_fill f "
            "JOIN fo_leg l ON l.id = f.leg_id JOIN fo_plan p ON p.id = l.plan_id "
            "WHERE p.plan_id = ? ORDER BY f.id", plan_id)  # fmt: skip

    def position(self, pid: int) -> dict[str, Any]:
        return self.rows("SELECT * FROM fo_position WHERE id = ?", pid)[0]

    def plan_row(self, plan_id: str) -> dict[str, Any]:
        return self.rows("SELECT * FROM fo_plan WHERE plan_id = ?", plan_id)[0]


@pytest.fixture
def world(conn: Any, user: int, tmp_path: Path) -> World:  # noqa: ANN401
    return World(conn, user, tmp_path)


def f1_scan(w: World) -> None:
    legs = [{"entry_seq": i, "role": r, "strike": str(STRIKES[r]), "option_type": TYPES[r],
             "expiry": NOV.isoformat(), "qty_sign": 1 if r.startswith("LONG") else -1}
            for i, r in enumerate(("LONG_PUT", "LONG_CALL", "SHORT_PUT", "SHORT_CALL"), 1)]
    w.scan("F1N", before(F1_ENTRY, 1), "NIFTY", {
        "underlying": "NIFTY", "entry_session": F1_ENTRY.isoformat(), "expiry": NOV.isoformat(),
        "hard_exit_date": HARD_EXIT.isoformat(), "legs": legs, "lot_size": LOT,
    })  # fmt: skip


def open_f1(w: World) -> tuple[str, int]:
    f1_scan(w)
    w.quotes.books.update(NEUTRAL)
    raised = w.tick(at(F1_ENTRY, 9, 20)).raised
    assert len(raised) == 1
    out = w.confirm(raised[0], at(F1_ENTRY, 9, 25))
    assert out.outcome == "OPEN", out
    assert out.position_id is not None
    return raised[0], out.position_id


def f2_scan(w: World) -> None:
    w.scan("F2", before(F2_ENTRY, 1), "RELIANCE", {
        "contract_expiry": NOV.isoformat(), "atr14": "20.00", "lot_size": F2_LOT,
        "industry": "Oil", "stop": "1340.00",
    }, rv20="0.250000")  # fmt: skip


def open_f2(w: World) -> tuple[str, int]:
    f2_scan(w)
    w.quotes.books[FUT_NOV] = book("1399.90", "1400.00", last="1400.00")
    raised = w.tick(at(F2_ENTRY, 9, 20)).raised
    assert len(raised) == 1
    out = w.confirm(raised[0], at(F2_ENTRY, 9, 22))
    assert out.outcome == "OPEN", out
    assert out.position_id is not None
    return raised[0], out.position_id


# --- F1 -------------------------------------------------------------------------------------------


@requires_db
class TestF1Replay:
    def test_the_0920_plan_from_the_scan_repriced_live_longs_first(self, world: World) -> None:
        f1_scan(world)
        world.quotes.books.update(NEUTRAL)
        assert world.tick(at(F1_ENTRY, 9, 19)).raised == []  # 09:20, not before
        raised = world.tick(at(F1_ENTRY, 9, 20)).raised
        assert raised == [M.entry_plan_id(FoSleeve.F1N, F1_ENTRY, "NIFTY", world.uid)]
        assert world.tick(at(F1_ENTRY, 9, 21)).raised == []  # a second raise is a no-op
        plan = world.plan_row(raised[0])
        assert (plan["status"], plan["kind"], plan["lots"], plan["lot_size"]) == (
            "ISSUED", "ENTRY", 1, LOT)
        assert X._aware(plan["expires_at"]) == at(F1_ENTRY, 9, 50)  # min(+30 min, 10:30)
        assert X._opt_day(plan["hard_exit_date"]) == HARD_EXIT
        assert Decimal(str(plan["credit_points"])) == Decimal("93.50")  # on live mids
        legs = world.rows(
            "SELECT entry_seq, role, side, quantity FROM fo_leg WHERE plan_id = ? "
            "ORDER BY entry_seq", plan["id"])  # fmt: skip
        assert [(lg["role"], lg["side"]) for lg in legs] == [
            ("LONG_PUT", "BUY"), ("LONG_CALL", "BUY"), ("SHORT_PUT", "SELL"),
            ("SHORT_CALL", "SELL")]  # fmt: skip
        assert {lg["quantity"] for lg in legs} == {LOT}
        assert world.fills(raised[0]) == []  # raised, never sent

    def test_a_thin_protecting_leg_abandons_before_any_short(self, world: World) -> None:
        f1_scan(world)
        world.quotes.books.update(NEUTRAL)
        plan_id = world.tick(at(F1_ENTRY, 9, 20)).raised[0]
        thin = world.quotes.books[opt_symbol("LONG_CALL")]
        world.quotes.books[opt_symbol("LONG_CALL")] = X.FoQuote(
            thin.bid, thin.ask, thin.bids, (Level(thin.ask or Decimal(0), 20),), thin.last, thin.oi)
        out = world.confirm(plan_id, at(F1_ENTRY, 9, 21))
        assert out.outcome == "ABANDONED_PARTIAL" and out.position_id is None
        fills = world.fills(plan_id)
        assert not any(f["role"].startswith("SHORT") for f in fills)  # no short ever sent
        # The long put filled and was sold back at once; the part-filled call too.
        net: dict[str, int] = {}
        for f in fills:
            net[f["role"]] = net.get(f["role"], 0) + (f["quantity"] if f["side"] == "BUY"
                                                      else -f["quantity"])  # fmt: skip
        assert set(net.values()) == {0}
        plan = world.plan_row(plan_id)
        assert plan["status"] == "ABANDONED_PARTIAL" and "abandoned" in plan["reason"]
        # Nothing is carried: no open position. The filled-and-closed legs cost real money, so
        # FO10.4 records them as one position opened and closed on its own fills, journalled.
        rows = world.rows("SELECT closed_at, closed_reason FROM fo_position WHERE user_id = ?",
                          world.uid)  # fmt: skip
        assert [(r["closed_at"] is not None, r["closed_reason"]) for r in rows] == [
            (True, "ABANDONED_PARTIAL")]  # fmt: skip
        assert world.kc.calls == []

    def test_rejections_carry_their_reason(self, world: World) -> None:
        f1_scan(world)
        world.quotes.books.update(NEUTRAL)
        world.margin = M.MarginQuote(Decimal(900000), Decimal(100000))
        plan = world.plan_row(world.tick(at(F1_ENTRY, 9, 20)).raised[0])
        assert plan["status"] == "REJECTED_MARGIN" and "margin" in plan["reason"]
        with pytest.raises(X.Refused):
            world.confirm(plan["plan_id"], at(F1_ENTRY, 9, 21))

    def test_a_wide_short_is_refused_by_name(self, world: World) -> None:
        f1_scan(world)
        world.quotes.books.update(NEUTRAL)
        world.quotes.books[opt_symbol("SHORT_CALL")] = book("55", "66")
        plan = world.plan_row(world.tick(at(F1_ENTRY, 9, 20)).raised[0])
        assert plan["status"] == "REJECTED_LIQUIDITY"
        assert "short call 25000" in plan["reason"] and "spread" in plan["reason"]

    def test_an_unconfirmed_plan_lapses(self, world: World) -> None:
        f1_scan(world)
        world.quotes.books.update(NEUTRAL)
        plan_id = world.tick(at(F1_ENTRY, 9, 20)).raised[0]
        for minute in range(21, 60):
            world.tick(at(F1_ENTRY, 9, minute))
        assert world.plan_row(plan_id)["status"] == "LAPSED"
        assert world.fills(plan_id) == []
        with pytest.raises(X.Refused) as caught:
            world.confirm(plan_id, at(F1_ENTRY, 9, 59))
        assert caught.value.status == 409

    def test_confirm_fills_longs_first_and_carries_with_nightly_marks(self, world: World) -> None:
        plan_id, pid = open_f1(world)
        fills = world.fills(plan_id)
        assert [f["role"] for f in fills] == ["LONG_PUT", "LONG_CALL", "SHORT_PUT", "SHORT_CALL"]
        assert all(f["simulated"] for f in fills)
        pos = world.position(pid)
        # Filled at the touch: shorts at the bid, wings at the ask: 60 + 55 - 12.5 - 10.5.
        assert Decimal(str(pos["entry_credit"])) == Decimal("92.00")
        assert pos["simulated"] is True and X._opt_day(pos["hard_exit_date"]) == HARD_EXIT
        assert world.plan_row(plan_id)["status"] == "OPEN"
        # The night: nothing until the bhavcopy lands, then each unmarked session once.
        nxt = before(HARD_EXIT, 13)
        assert world.night(nxt).landed is False
        world.market.land_condor(F1_ENTRY, {"SHORT_CALL": "58", "SHORT_PUT": "54",
                                            "LONG_CALL": "12", "LONG_PUT": "10"})  # fmt: skip
        world.market.land_condor(nxt, {"SHORT_CALL": "50", "SHORT_PUT": "50",
                                       "LONG_CALL": "10", "LONG_PUT": "9"})  # fmt: skip
        assert world.night(nxt).marked == 2  # both sessions, the missed one caught up
        assert world.night(nxt).marked == 0  # idempotent
        marks = world.rows("SELECT trade_date, mark_points, pnl_inr FROM fo_mark "
                           "WHERE position_id = ? ORDER BY trade_date", pid)  # fmt: skip
        assert [(X._day(m["trade_date"]), Decimal(str(m["mark_points"])),
                 Decimal(str(m["pnl_inr"]))) for m in marks] == [
            (F1_ENTRY, Decimal("90.00"), Decimal("130.00")),  # (92 - 90) x 65
            (nxt, Decimal("81.00"), Decimal("715.00")),
        ]  # fmt: skip
        assert world.kc.calls == []

    def test_profit_take_on_live_mids_closes_shorts_first(self, world: World) -> None:
        plan_id, pid = open_f1(world)
        day = before(HARD_EXIT, 5)
        assert world.tick(at(day, 11, 0)).actions == []  # neutral book: nothing due
        world.quotes.books.update(condor_book("20", "18", "3", "2"))  # cost to close 33 <= 46
        report = world.tick(at(day, 11, 1))
        assert report.actions == [f"{plan_id}-X"]
        exit_plan = world.plan_row(f"{plan_id}-X")
        assert (exit_plan["kind"], exit_plan["status"], exit_plan["parent_plan_id"]) == (
            "EXIT", "CLOSED", plan_id)
        assert exit_plan["confirmed_at"] is not None  # the entry's confirm, not a new one
        assert X._json(exit_plan["detail"])["code"] == "PROFIT_TAKE"
        assert [f["role"] for f in world.fills(f"{plan_id}-X")] == [
            "SHORT_CALL", "SHORT_PUT", "LONG_CALL", "LONG_PUT"]
        pos = world.position(pid)
        assert pos["closed_at"] is not None and pos["closed_reason"] == "PROFIT_TAKE"
        assert world.plan_row(plan_id)["status"] == "CLOSED"
        assert world.kc.calls == []

    def test_loss_close_at_two_and_a_half_credits(self, world: World) -> None:
        plan_id, pid = open_f1(world)
        day = before(HARD_EXIT, 8)
        world.quotes.books.update(condor_book("200", "20", "30", "5"))  # 170 + 15 = 185 < 230
        assert world.tick(at(day, 13, 0)).actions == []
        world.quotes.books.update(condor_book("250", "20", "30", "5"))  # 220 + 15 = 235 >= 230
        assert world.tick(at(day, 13, 1)).actions == [f"{plan_id}-X"]
        assert world.position(pid)["closed_reason"] == "LOSS_CLOSE"
        assert [f["role"] for f in world.fills(f"{plan_id}-X")][:2] == ["SHORT_CALL", "SHORT_PUT"]

    def test_the_e_minus_1_exit_at_1500(self, world: World) -> None:
        plan_id, pid = open_f1(world)
        assert world.tick(at(HARD_EXIT, 14, 59)).actions == []
        assert world.tick(at(HARD_EXIT, 15, 0)).actions == [f"{plan_id}-X"]
        assert world.position(pid)["closed_reason"] == "HARD_EXIT"
        assert X._json(world.plan_row(plan_id)["detail"]).get("violations") == []

    def test_a_hard_exit_passed_while_down_is_a_late_exit_at_the_next_open(
        self, world: World
    ) -> None:
        plan_id, pid = open_f1(world)
        assert world.tick(at(NOV, 9, 15)).actions == [f"{plan_id}-X"]  # expiry day's open
        assert world.position(pid)["closed_reason"] == "LATE_EXIT"
        violations = X._json(world.plan_row(plan_id)["detail"])["violations"]
        assert [v["code"] for v in violations] == ["LATE_EXIT"]
        assert any("LATE_EXIT" in a for a in world.alerts)


# --- F2 -------------------------------------------------------------------------------------------


@requires_db
class TestF2Replay:
    def test_a_month_entry_trail_roll_and_time_exit(self, world: World) -> None:
        plan_id, pid = open_f2(world)
        plan = world.plan_row(plan_id)
        detail = X._json(plan["detail"])
        assert (plan["lots"], plan["sizing_mode"]) == (1, "PAPER_ONE_LOT")
        assert detail["lots_at_ceiling"] == 0  # one lot risks ₹30,000 > the ₹25,000 ceiling
        pos = world.position(pid)
        assert Decimal(str(pos["entry_price"])) == Decimal("1400.00")
        assert Decimal(str(pos["stop_price"])) == Decimal("1340.00")  # 1400 - 3 x 20
        assert pos["gtt_id"] == f"PAPER-{pid}"  # placed in the fill's session
        assert X._opt_day(pos["next_roll_date"]) == HARD_EXIT
        # Evening 1: a new high moves the stop to 1450 - 60 and the GTT with it.
        world.market.land_future(F2_ENTRY, NOV, close="1450", settle="1449")
        night = world.night(F2_ENTRY)
        assert night.trailed == ["RELIANCE:1340.00->1390.00:DRY_RUN_GTT_MODIFY"]
        assert Decimal(str(world.position(pid)["stop_price"])) == Decimal("1390.00")
        journal = (world.tmp / "fo-F2.jsonl").read_text().splitlines()
        modify = [json.loads(x) for x in journal if "gtt_dry_run_modify" in x]
        assert modify and modify[-1]["product"] == "NRML" and modify[-1]["floor_trigger"] == 1340
        # Evening 2: a lower close never lowers it.
        day2 = SESSIONS[SESSIONS.index(F2_ENTRY) + 1]
        world.market.land_future(day2, NOV, close="1420", settle="1421")
        assert world.night(day2).trailed == []
        assert Decimal(str(world.position(pid)["stop_price"])) == Decimal("1390.00")
        marks = world.rows("SELECT mark_points, pnl_inr FROM fo_mark WHERE position_id = ? "
                           "ORDER BY trade_date", pid)  # fmt: skip
        assert [Decimal(str(m["pnl_inr"])) for m in marks] == [Decimal("24500.00"),
                                                               Decimal("10500.00")]
        # E-1 15:00: the roll, same quantity, stop carried, under the original confirm.
        world.quotes.books[FUT_NOV] = book("1430.00", "1430.10", last="1430.05")
        world.quotes.books[FUT_DEC] = book("1438.00", "1438.10", last="1438.05")
        assert world.tick(at(HARD_EXIT, 14, 59)).actions == []
        assert world.tick(at(HARD_EXIT, 15, 0)).actions == [f"{plan_id}-R1"]
        roll = world.plan_row(f"{plan_id}-R1")
        assert (roll["kind"], roll["status"], roll["parent_plan_id"]) == ("ROLL", "OPEN", plan_id)
        assert [(f["side"], f["quantity"]) for f in world.fills(f"{plan_id}-R1")] == [
            ("SELL", F2_LOT), ("BUY", F2_LOT)]
        pos = world.position(pid)
        assert pos["closed_at"] is None
        assert X._json(pos["legs"])["legs"][0]["tradingsymbol"] == FUT_DEC
        assert X._json(pos["legs"])["carry"]["rolls"] == 1
        assert Decimal(str(pos["stop_price"])) == Decimal("1390.00")  # carried
        assert X._opt_day(pos["next_roll_date"]) == before(DEC, 1)
        assert pos["gtt_id"] == f"PAPER-{pid}"
        # The 40th session of the hold, 15:00: the time exit (before December's roll).
        time_exit = SESSIONS[SESSIONS.index(F2_ENTRY) + 39]
        assert time_exit < before(DEC, 1)
        assert world.tick(at(time_exit, 14, 59)).actions == []
        assert world.tick(at(time_exit, 15, 0)).actions == [f"{plan_id}-X"]
        assert world.position(pid)["closed_reason"] == "TIME_EXIT"
        assert world.fills(f"{plan_id}-X")[0]["side"] == "SELL"
        assert world.kc.calls == []

    def test_a_stop_out(self, world: World) -> None:
        plan_id, pid = open_f2(world)
        day = before(HARD_EXIT, 10)
        world.quotes.books[FUT_NOV] = book("1345", "1345.10", last="1345")
        assert world.tick(at(day, 10, 0)).actions == []
        world.quotes.books[FUT_NOV] = book("1338", "1338.10", last="1339.95")
        assert world.tick(at(day, 10, 1)).actions == [f"{plan_id}-X"]
        pos = world.position(pid)
        assert pos["closed_reason"] == "STOP" and pos["closed_at"] is not None
        assert world.kc.calls == []

    def test_a_gtt_that_fails_is_naked_and_exits_at_the_next_check(self, world: World) -> None:
        f2_scan(world)
        world.quotes.books[FUT_NOV] = book("1399.90", "1400.00", last="1400.00")
        plan_id = world.tick(at(F2_ENTRY, 9, 20)).raised[0]
        gw = world.gateway_for(FoSleeve.F2)

        async def refuse(**_: object) -> dict[str, object]:
            return {"status": "GTT_ERROR", "error": "fixture: the broker refused the trigger"}

        gw.place_gtt_stop = refuse
        out = world.confirm(plan_id, at(F2_ENTRY, 9, 22))
        assert out.outcome == "OPEN" and out.position_id is not None
        assert world.position(out.position_id)["gtt_id"] is None
        assert any("GTT stop did not place" in a for a in world.alerts)
        violations = X._json(world.plan_row(plan_id)["detail"])["violations"]
        assert [v["code"] for v in violations] == ["NAKED_FUTURE"]
        assert world.tick(at(F2_ENTRY, 9, 23)).actions == [f"{plan_id}-X"]
        assert world.position(out.position_id)["closed_reason"] == "NAKED_FUTURE"


# --- gating ---------------------------------------------------------------------------------------


class TestGating:
    def test_the_flag_off_builds_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert C.FNO_MONITOR_ENABLED is False
        assert M.build_monitor(enabled=False, store=None) is None
        monkeypatch.setattr(C, "FNO_MONITOR_ENABLED", False)
        assert M.main() == 0

    def test_there_is_no_auto_flag_and_the_monitor_never_confirms(self) -> None:
        for module in (M, X):
            src = inspect.getsource(module)
            assert not re.search(r"FNO_\w*AUTO", src), module.__name__
        monitor_src = inspect.getsource(M)
        assert "execute_entry" not in monitor_src.replace("``fno_execute.execute_entry", "")
        assert ".confirm(" not in monitor_src

    @requires_db
    def test_no_path_forms_an_entry_without_a_confirm(self, world: World) -> None:
        f1_scan(world)
        f2_scan(world)
        world.quotes.books.update(NEUTRAL)
        world.quotes.books[FUT_NOV] = book("1399.90", "1400.00", last="1400.00")
        for minute in range(20, 60):
            world.tick(at(F1_ENTRY, 9, minute))
        assert world.rows("SELECT count(*) AS n FROM fo_fill WHERE user_id = ?",
                          world.uid)[0]["n"] == 0  # fmt: skip
        plan_id = world.rows("SELECT plan_id FROM fo_plan WHERE user_id = ? AND sleeve = 'F1N'",
                             world.uid)[0]["plan_id"]  # fmt: skip
        with pytest.raises(X.Refused) as caught:
            world.confirm(plan_id, at(F1_ENTRY, 9, 21), confirm=False)
        assert caught.value.code == "CONFIRM_REQUIRED"
        assert world.kc.calls == []

    @requires_db
    def test_every_flag_false_is_simulated_and_the_broker_sees_nothing(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in ("OPTIONS_ENABLED", "FNO_CARRY_ENABLED", "FNO_F1_EXECUTION_ENABLED",
                     "FNO_F2_EXECUTION_ENABLED"):  # fmt: skip
            monkeypatch.setattr(C, name, False)
        for dry in (True, False):
            monkeypatch.setattr(C, "DRY_RUN", dry)
            assert X.paper_gates(FoSleeve.F1N).dry_run is True
        _plan_id, pid = open_f1(world)
        world.tick(at(HARD_EXIT, 15, 0))
        assert world.position(pid)["closed_reason"] == "HARD_EXIT"
        simulated = world.rows("SELECT bool_and(simulated) AS s, count(*) AS n FROM fo_fill "
                               "WHERE user_id = ?", world.uid)[0]  # fmt: skip
        assert (simulated["s"], simulated["n"]) == (True, 8)
        assert world.kc.calls == []

    @requires_db
    def test_the_paper_pin_holds_with_every_flag_on(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _plan_id, pid = open_f1(world)  # confirmed on paper
        for name in ("OPTIONS_ENABLED", "FNO_CARRY_ENABLED", "FNO_F1_EXECUTION_ENABLED"):
            monkeypatch.setattr(C, name, True)
        monkeypatch.setattr(C, "DRY_RUN", False)
        assert X.fno_mode(FoSleeve.F1N).value == "LIVE"
        assert X.paper_gates(FoSleeve.F1N).dry_run is True  # FO7 builds no live path
        world.tick(at(HARD_EXIT, 15, 0))  # the exit still runs, on paper
        assert world.position(pid)["closed_reason"] == "HARD_EXIT"
        assert world.kc.calls == []


@requires_db
class TestPgMarket:
    """The nightly's reads of the shared market tables. A Saturday and a symbol no feed prints,
    removed afterwards, so the shared test database is left as found."""

    DAY = dt.date(2022, 2, 19)

    def test_landed_prints_and_the_calendar(self, conn: Any) -> None:  # noqa: ANN401
        market = M.PgMarket(conn)
        try:
            conn.execute(
                "INSERT INTO fo_contract_daily (trade_date, instrument, symbol, expiry, strike, "
                "option_type, close, settle, source_key) VALUES "
                "(?, 'FUTSTK', 'FO7TEST', ?, 0, 'XX', 101.5, 101.25, 'fo7-test'), "
                "(?, 'OPTSTK', 'FO7TEST', ?, 100, 'CE', 3.5, 3.45, 'fo7-test')",
                (self.DAY, dt.date(2022, 2, 24), self.DAY, dt.date(2022, 2, 24)),
            )
            assert market.landed(self.DAY) is False  # rows alone are not an ingested night
            conn.execute(
                "INSERT INTO fo_ingest_day (trade_date, status) VALUES (?, 'INGESTED')", (self.DAY,)
            )
            assert market.landed(self.DAY) is True
            fut: M.PrintKey = ("FO7TEST", dt.date(2022, 2, 24), None, "XX")
            opt: M.PrintKey = ("FO7TEST", dt.date(2022, 2, 24), Decimal(100), "CE")
            gone: M.PrintKey = ("FO7TEST", dt.date(2022, 2, 24), Decimal(200), "CE")
            prints = market.prints(self.DAY, [fut, opt, gone])
            assert prints[fut] == M.Print(Decimal("101.5"), Decimal("101.25"))
            assert prints[opt].settle == Decimal("3.45") and gone not in prints
            sessions = market.sessions()
            assert dt.date(2022, 2, 18) in sessions and self.DAY not in sessions
        finally:
            conn.execute("DELETE FROM fo_contract_daily WHERE symbol = 'FO7TEST'")
            conn.execute("DELETE FROM fo_ingest_day WHERE trade_date = ?", (self.DAY,))
