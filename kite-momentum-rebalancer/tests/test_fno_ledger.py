"""FO10 — the FO journal and ``04`` §7's pauses on the desk (``docs/fno/06`` FO10).

Replayed on the FO7 world (`tests.test_fno_monitor`: the real monitor, executor and paper-pinned
gateway over the test Postgres, a broker that must see nothing):

* a ``fo_journal`` row at **every close kind** — profit take, loss close, hard exit, LATE_EXIT,
  stop, time exit after a roll (the roll journalled with its own costs), NAKED_FUTURE, and an
  abandoned entry — in ₹ and R, from the fills, costs itemised, ``simulated`` carried;
* the pauses at ``04`` §7's exact thresholds, written with an audit row, stopping new entries only
  (``REJECTED_PAUSED``), and **never pooled**: a live loss never pauses paper, nor paper live.

Skipped without ``BASKFY_TEST_DATABASE_URL``.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from app import fno_execute as X
from app import fno_ledger as L

from baskfy_core.fno.config import FoSleeve
from baskfy_core.options.chain import Level
from baskfy_core.options.config import CostRates, Side
from baskfy_core.options.costs import CostFill, charges
from tests import test_fno_monitor as fo7
from tests.test_fno_monitor import (
    F1_ENTRY,
    F2_ENTRY,
    F2_LOT,
    FUT_DEC,
    FUT_NOV,
    HARD_EXIT,
    NOV,
    SESSIONS,
    World,
    at,
    before,
    book,
    condor_book,
    f1_scan,
    f2_scan,
    open_f1,
    open_f2,
    opt_symbol,
    requires_db,
)

# FO7's fixtures, re-exported so pytest finds them here: the database, a user, the world.
conn = fo7.conn
user = fo7.user
world = fo7.world

D = Decimal
IST = X.IST


def journal(w: World, pid: int) -> dict[str, Any]:
    rows = w.rows("SELECT * FROM fo_journal WHERE position_id = ?", pid)
    assert len(rows) == 1, f"one journal row for position {pid}"
    return rows[0]


def dec(value: object) -> Decimal:
    return Decimal(str(value))


def fills_of(w: World, pid: int) -> list[dict[str, Any]]:
    return w.rows("SELECT side, price, quantity FROM fo_fill WHERE position_id = ? ORDER BY id",
                  pid)  # fmt: skip


def assert_in_rupees_and_r(w: World, pid: int) -> dict[str, Any]:
    """The row's arithmetic is its fills': gross = signed cash, net = gross - costs, R = net ÷
    the position's planned max loss."""
    row = journal(w, pid)
    cash = sum((dec(f["price"]) * f["quantity"] * (1 if f["side"] == "SELL" else -1)
                for f in fills_of(w, pid)), D(0))  # fmt: skip
    assert dec(row["gross_pnl_inr"]) == cash.quantize(D("0.01"))
    assert dec(row["net_pnl_inr"]) == dec(row["gross_pnl_inr"]) - dec(row["costs_inr"])
    assert dec(row["costs_inr"]) > 0
    r = dec(w.position(pid)["max_loss_inr"])
    assert dec(row["risk_budget_inr"]) == r
    assert dec(row["r_multiple"]) == (dec(row["net_pnl_inr"]) / r).quantize(D("0.01"))
    assert row["simulated"] is True
    costs = X._json(row["detail"])["costs"]
    assert D(costs["total"]) == dec(row["costs_inr"])
    return row


# --- a journal row at every close -----------------------------------------------------------------


@requires_db
class TestF1Closes:
    def test_profit_take(self, world: World) -> None:
        plan_id, pid = open_f1(world)
        world.quotes.books.update(condor_book("20", "18", "3", "2"))
        world.tick(at(before(HARD_EXIT, 5), 11, 0))
        row = assert_in_rupees_and_r(world, pid)
        assert row["closed_reason"] == "PROFIT_TAKE" and row["structure"] == "IRON_CONDOR"
        costs = X._json(row["detail"])["costs"]
        assert (costs["entry"]["orders"], costs["exit"]["orders"]) == (4, 4)  # 8 orders
        entry = [CostFill(Side(f["side"]), dec(f["price"]), f["quantity"])
                 for f in world.fills(plan_id)]  # fmt: skip
        assert D(costs["entry"]["total"]) == charges(entry, CostRates()).total
        assert dec(row["net_pnl_inr"]) > 0 and dec(row["r_multiple"]) > 0
        assert row["sizing_mode"] == world.plan_row(plan_id)["sizing_mode"]
        assert row["sessions_held"] >= 1 and row["rolls"] == 0
        assert world.kc.calls == []

    def test_loss_close(self, world: World) -> None:
        _plan_id, pid = open_f1(world)
        world.quotes.books.update(condor_book("250", "20", "30", "5"))
        world.tick(at(before(HARD_EXIT, 8), 13, 1))
        row = assert_in_rupees_and_r(world, pid)
        assert row["closed_reason"] == "LOSS_CLOSE" and dec(row["r_multiple"]) < 0

    def test_hard_exit(self, world: World) -> None:
        _plan_id, pid = open_f1(world)
        world.tick(at(HARD_EXIT, 15, 0))
        assert assert_in_rupees_and_r(world, pid)["closed_reason"] == "HARD_EXIT"

    def test_late_exit_carries_its_violation(self, world: World) -> None:
        _plan_id, pid = open_f1(world)
        world.tick(at(NOV, 9, 15))
        row = assert_in_rupees_and_r(world, pid)
        assert row["closed_reason"] == "LATE_EXIT"
        assert [v["code"] for v in X._json(row["detail"])["violations"]] == ["LATE_EXIT"]

    def test_an_abandoned_entry_is_journalled_and_carries_nothing(self, world: World) -> None:
        f1_scan(world)
        world.quotes.books.update(condor_book("60.5", "55.5", "12.25", "10.25"))
        plan_id = world.tick(at(F1_ENTRY, 9, 20)).raised[0]
        thin = world.quotes.books[opt_symbol("LONG_CALL")]
        world.quotes.books[opt_symbol("LONG_CALL")] = X.FoQuote(
            thin.bid, thin.ask, thin.bids, (Level(thin.ask or D(0), 20),), thin.last, thin.oi)
        assert world.confirm(plan_id, at(F1_ENTRY, 9, 21)).outcome == "ABANDONED_PARTIAL"
        pos = world.rows("SELECT * FROM fo_position WHERE user_id = ?", world.uid)
        assert len(pos) == 1 and pos[0]["closed_at"] is not None
        row = journal(world, int(pos[0]["id"]))
        assert row["closed_reason"] == "ABANDONED_PARTIAL"
        assert dec(row["net_pnl_inr"]) < 0  # the round trip's spread and charges
        assert world.store.open_positions() == []


@requires_db
class TestF2Closes:
    def test_a_roll_then_the_time_exit_journals_the_roll_with_its_own_costs(
        self, world: World
    ) -> None:
        plan_id, pid = open_f2(world)
        world.quotes.books[FUT_NOV] = book("1430.00", "1430.10", last="1430.05")
        world.quotes.books[FUT_DEC] = book("1438.00", "1438.10", last="1438.05")
        world.tick(at(HARD_EXIT, 15, 0))
        assert world.rows("SELECT count(*) AS n FROM fo_journal WHERE position_id = ?",
                          pid)[0]["n"] == 0  # a roll is not a close  # fmt: skip
        time_exit = SESSIONS[SESSIONS.index(F2_ENTRY) + 39]
        world.tick(at(time_exit, 15, 0))
        row = assert_in_rupees_and_r(world, pid)
        assert (row["closed_reason"], row["rolls"], row["structure"]) == ("TIME_EXIT", 1, "FUTURE")
        costs = X._json(row["detail"])["costs"]
        [roll] = costs["rolls"]
        assert (roll["plan_id"], roll["costs"]["orders"]) == (f"{plan_id}-R1", 2)
        assert D(roll["sold_inr"]) == D("1430.00") * F2_LOT
        assert D(costs["total"]) == (D(costs["entry"]["total"]) + D(costs["exit"]["total"])
                                     + D(roll["costs"]["total"]))  # fmt: skip
        assert world.kc.calls == []

    def test_a_stop_out(self, world: World) -> None:
        _plan_id, pid = open_f2(world)
        world.quotes.books[FUT_NOV] = book("1338", "1338.10", last="1339.95")
        world.tick(at(before(HARD_EXIT, 10), 10, 1))
        row = assert_in_rupees_and_r(world, pid)
        assert row["closed_reason"] == "STOP" and dec(row["r_multiple"]) < 0

    def test_a_naked_future(self, world: World) -> None:
        f2_scan(world)
        world.quotes.books[FUT_NOV] = book("1399.90", "1400.00", last="1400.00")
        plan_id = world.tick(at(F2_ENTRY, 9, 20)).raised[0]
        gw = world.gateway_for(FoSleeve.F2)

        async def refuse(**_: object) -> dict[str, object]:
            return {"status": "GTT_ERROR", "error": "fixture: refused"}

        gw.place_gtt_stop = refuse
        pid = world.confirm(plan_id, at(F2_ENTRY, 9, 22)).position_id
        assert pid is not None
        world.tick(at(F2_ENTRY, 9, 23))
        row = assert_in_rupees_and_r(world, pid)
        assert row["closed_reason"] == "NAKED_FUTURE"
        assert [v["code"] for v in X._json(row["detail"])["violations"]] == ["NAKED_FUTURE"]


# --- the pauses -----------------------------------------------------------------------------------


def closed(  # noqa: PLR0913 - one journal row, by keyword
    w: World, sleeve: str, r: str, *, inr: str | None = None, simulated: bool = True,
    on: dt.date | None = None,
) -> int:  # fmt: skip
    """One closed position and its journal row (a trade the ledger reads)."""
    day = on or before(F1_ENTRY, 3)
    symbol = {"F1N": "NIFTY", "F1B": "BANKNIFTY"}.get(sleeve, "RELIANCE")
    structure = "FUTURE" if sleeve == "F2" else "IRON_CONDOR"
    at_close = dt.datetime.combine(day, dt.time(15, 0), tzinfo=IST)
    pid = int(w.conn.execute(
        "INSERT INTO fo_position (user_id, sleeve, symbol, structure, entry_plan_id, legs, lots, "
        "lot_size, max_loss_inr, opened_at, closed_at, closed_reason, simulated) VALUES "
        "(?, ?, ?, ?, ?, '{}', 1, 65, 10000, ?, ?, 'LOSS_CLOSE', ?) RETURNING id",
        (w.uid, sleeve, symbol, structure, f"seed-{sleeve}-{r}-{day}", at_close, at_close,
         simulated),
    ).fetchone()["id"])  # fmt: skip
    net = D(inr) if inr is not None else D(r) * 10000
    w.conn.execute(
        "INSERT INTO fo_journal (position_id, user_id, sleeve, symbol, structure, opened_on, "
        "closed_on, entry_inr, exit_inr, gross_pnl_inr, costs_inr, net_pnl_inr, "
        "risk_budget_inr, r_multiple, closed_reason, sessions_held, simulated, sizing_mode) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?, 0, ?, 10000, ?, 'LOSS_CLOSE', 1, ?, 'BUDGET')",
        (pid, w.uid, sleeve, symbol, structure, day, day, net, net, D(r), simulated),
    )
    return pid


def audits(w: World, key: str) -> list[dict[str, Any]]:
    return w.rows("SELECT * FROM fo_config_audit WHERE user_id = ? AND key = ? ORDER BY id",
                  w.uid, key)  # fmt: skip


@requires_db
class TestPauses:
    def test_three_f1_closes_at_minus_0_6r_refuse_that_underlyings_next_entry(
        self, world: World
    ) -> None:
        for i, r in enumerate(("-0.60", "-0.73", "-0.60")):
            closed(world, "F1N", r, on=before(F1_ENTRY, 5 - i))
        found = L.apply_pauses(world.store, simulated=True, today=F1_ENTRY)
        assert [(p.scope, p.reason) for p in found] == [("F1N", "PAPER:F1_LOSS_RUN")]
        assert len(audits(world, "pause:F1N")) == 1
        L.apply_pauses(world.store, simulated=True, today=F1_ENTRY)
        assert len(audits(world, "pause:F1N")) == 1  # once per run
        f1_scan(world)
        world.quotes.books.update(condor_book("60.5", "55.5", "12.25", "10.25"))
        [plan_id] = world.tick(at(F1_ENTRY, 9, 20)).raised
        plan = world.plan_row(plan_id)
        assert plan["status"] == "REJECTED_PAUSED" and "-0.6R" in plan["reason"]
        assert world.fills(plan_id) == [] and world.kc.calls == []
        assert L.entry_block(world.store, FoSleeve.F1B, day=F1_ENTRY, simulated=True) == []

    def test_minus_0_59r_is_not_the_loss_close(self, world: World) -> None:
        for i, r in enumerate(("-0.60", "-0.59", "-0.73")):
            closed(world, "F1N", r, on=before(F1_ENTRY, 5 - i))
        assert L.entry_block(world.store, FoSleeve.F1N, day=F1_ENTRY, simulated=True) == []

    def test_a_pause_stops_entries_only(self, world: World) -> None:
        _plan_id, pid = open_f1(world)
        for i in range(3):
            closed(world, "F1N", "-0.7", on=before(F1_ENTRY, 3 - i))
        assert L.entry_block(world.store, FoSleeve.F1N, day=HARD_EXIT, simulated=True)
        world.tick(at(HARD_EXIT, 15, 0))  # the open structure runs to its own exit
        assert world.position(pid)["closed_reason"] == "HARD_EXIT"

    def test_f2_at_minus_6r_pauses_to_the_month_end_and_is_audited(self, world: World) -> None:
        day = dt.date(2026, 11, 20)
        for i, r in enumerate(("-2", "-2", "-2")):
            closed(world, "F2", r, on=dt.date(2026, 11, 2 + i))
        L.apply_pauses(world.store, simulated=True, today=day)
        row = world.rows("SELECT paused_until, paused_reason FROM fo_sleeve_config "
                         "WHERE user_id = ? AND sleeve = 'F2'", world.uid)[0]  # fmt: skip
        assert (X._opt_day(row["paused_until"]), row["paused_reason"]) == (
            dt.date(2026, 11, 30), "PAPER:F2_MONTH_R")
        [audit] = audits(world, "paused_until")
        assert (audit["scope"], audit["changed_by"], audit["new_value"]) == (
            "F2", "ledger", "2026-11-30")
        L.apply_pauses(world.store, simulated=True, today=day)
        assert len(audits(world, "paused_until")) == 1  # idempotent
        assert L.entry_block(world.store, FoSleeve.F2, day=day, simulated=True)
        assert not L.entry_block(world.store, FoSleeve.F2, day=dt.date(2026, 12, 1),
                                 simulated=True)  # fmt: skip

    def test_f2_at_minus_5_99r_does_not(self, world: World) -> None:
        for i, r in enumerate(("-2", "-2", "-1.99")):
            closed(world, "F2", r, on=dt.date(2026, 11, 2 + i))
        assert L.apply_pauses(world.store, simulated=True, today=dt.date(2026, 11, 20)) == []

    def test_the_book_at_the_75000_ceiling_when_the_amount_is_0(self, world: World) -> None:
        day = dt.date(2026, 11, 20)
        closed(world, "F1N", "-0.5", inr="-74999.99", on=dt.date(2026, 11, 2))
        assert L.apply_pauses(world.store, simulated=True, today=day) == []
        closed(world, "F2", "-0.01", inr="-0.01", on=dt.date(2026, 11, 3))
        found = L.apply_pauses(world.store, simulated=True, today=day)
        assert [(p.scope, p.reason) for p in found] == [("BOOK", "PAPER:BOOK_MONTH_INR")]
        row = world.rows("SELECT paused_until, paused_reason FROM fo_book_config WHERE "
                         "user_id = ?", world.uid)[0]  # fmt: skip
        assert X._opt_day(row["paused_until"]) == dt.date(2026, 11, 30)
        assert L.entry_block(world.store, FoSleeve.F1B, day=day, simulated=True)

    def test_a_set_book_amount_is_the_limit(self, world: World) -> None:
        world.conn.execute("INSERT INTO fo_book_config (user_id, monthly_pause_inr) VALUES (?, "
                           "30000)", (world.uid,))  # fmt: skip
        closed(world, "F2", "-1", inr="-30000.00", on=dt.date(2026, 11, 2))
        found = L.apply_pauses(world.store, simulated=True, today=dt.date(2026, 11, 20))
        assert [p.scope for p in found] == ["BOOK"]


@requires_db
class TestNeverPooled:
    def test_a_live_loss_never_pauses_paper(self, world: World) -> None:
        for i in range(3):
            closed(world, "F1N", "-0.9", inr="-3000", simulated=False,
                   on=before(F1_ENTRY, 5 - i))  # fmt: skip
        closed(world, "F2", "-9", inr="-90000", simulated=False, on=before(F1_ENTRY, 1))
        live = L.nightly_pauses(world.store, today=F1_ENTRY)
        assert {(p.scope, p.simulated) for p in live} == {
            ("F1N", False), ("F2", False), ("BOOK", False)}  # fmt: skip
        book_row = world.rows("SELECT paused_reason FROM fo_book_config WHERE user_id = ?",
                              world.uid)[0]  # fmt: skip
        assert book_row["paused_reason"] == "LIVE:BOOK_MONTH_INR"
        for sleeve in (FoSleeve.F1N, FoSleeve.F2):
            assert L.entry_block(world.store, sleeve, day=F1_ENTRY, simulated=True) == []
            assert L.entry_block(world.store, sleeve, day=F1_ENTRY, simulated=False)
        _plan_id, pid = open_f1(world)  # paper raises and opens all the same
        assert world.position(pid)["simulated"] is True

    def test_a_paper_loss_never_pauses_live(self, world: World) -> None:
        for i in range(3):
            closed(world, "F1N", "-0.9", inr="-30000", on=before(F1_ENTRY, 5 - i))
        L.nightly_pauses(world.store, today=F1_ENTRY)
        assert L.entry_block(world.store, FoSleeve.F1N, day=F1_ENTRY, simulated=True)
        assert L.entry_block(world.store, FoSleeve.F1N, day=F1_ENTRY, simulated=False) == []

    def test_a_hand_set_pause_binds_both(self, world: World) -> None:
        world.conn.execute("UPDATE fo_sleeve_config SET paused_until = ?, paused_reason = "
                           "'Maulik' WHERE user_id = ? AND sleeve = 'F1'",
                           (F1_ENTRY, world.uid))  # fmt: skip
        for simulated in (True, False):
            assert L.entry_block(world.store, FoSleeve.F1B, day=F1_ENTRY, simulated=simulated)


@requires_db
def test_the_night_marks_then_records_the_pauses(world: World) -> None:
    for i, r in enumerate(("-2", "-2", "-2")):
        closed(world, "F2", r, on=dt.date(2026, 11, 2 + i))
    world.market.landed_days.add(HARD_EXIT)
    assert world.night(HARD_EXIT).pauses == ["PAPER:F2_MONTH_R:F2"]
