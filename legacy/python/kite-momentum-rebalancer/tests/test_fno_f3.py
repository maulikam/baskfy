"""F3-5: the desk's F3 replay — the directional index credit spread (``gates/f3-5-desk.md``;
``docs/fno/04`` §11; DECISIONS-FO M.5, Maulik's three answers, 28 Sep 2026).

On ``test_fno_monitor``'s world: the scan's CANDIDATE row → the 09:20 plan (wing first, short
second, priced on mids, the intraday check and the level from the index quote) → the confirm →
the position with its level → the watch (a level break, the cut, the target, the hard exit) →
the exit ISSUED for the click with the flag off and sent by the monitor with it on → the add
the next session → the nightly mark. The spy broker records no call anywhere.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app import fno_desk as DESK
from app import fno_execute as X
from app import fno_gates as G
from app import fno_monitor as M
from baskfy_core.fno.config import FoSleeve
from test_fno_monitor import (
    HARD_EXIT,
    LOT,
    NOV,
    STRIKES,
    FakeInstruments,
    World,
    at,
    before,
    book,
    conn,  # noqa: F401 - the module-scoped fixture
    opt_symbol,
    requires_db,
    user,  # noqa: F401 - the per-test fixture
)

#: An F3 entry session inside the fixture calendar, and the weekly it trades (the fixture's NOV).
ENTRY = before(NOV, 12)
LEVEL = Decimal("24000")
SHORT = "SHORT_PUT"
WING = "LONG_PUT"


def spread_book(short_mid: str, wing_mid: str) -> dict[str, X.FoQuote]:
    s, w = Decimal(short_mid), Decimal(wing_mid)
    return {
        opt_symbol(SHORT): book(str(s - Decimal("0.5")), str(s + Decimal("0.5"))),
        opt_symbol(WING): book(str(w - Decimal("0.25")), str(w + Decimal("0.25"))),
    }


class F3World(World):
    """The monitor with F3's two seams: the index quote and the flag."""

    def __init__(self, conn: Any, uid: int, tmp: Path) -> None:  # noqa: ANN401
        super().__init__(conn, uid, tmp)
        self.index: M.IndexQuote | None = M.IndexQuote(Decimal("24300"), Decimal("24200"))
        self.auto_exit = False
        self.monitor = M.FnoMonitor(
            store=self.store, gateway_for=self.gateway_for, quotes=self.quotes,
            instruments=FakeInstruments(), margins=lambda _legs: self.margin,
            market=self.market, alert=self.alerts.append,
            index_quotes=lambda _u: self.index, exit_by_monitor=lambda _s: self.auto_exit,
        )  # fmt: skip

    def click_exit(self, plan_id: str, now: dt.datetime) -> X.FoOutcome:
        async def go() -> X.FoOutcome:
            return await X.execute_click_exit(
                self.store, self.gateway_for(FoSleeve.F3N), quotes=self.quotes, plan_id=plan_id,
                confirm=True, now=lambda: now,
            )  # fmt: skip

        return asyncio.run(go())


@pytest.fixture
def w(conn: Any, user: int, tmp_path: Path) -> F3World:  # noqa: ANN401
    return F3World(conn, user, tmp_path)


def f3_scan(w: World, *, state: str = "CANDIDATE", trade_date: dt.date | None = None) -> None:
    legs = [
        {"entry_seq": 1, "role": WING, "strike": str(STRIKES[WING]), "option_type": "PE",
         "expiry": NOV.isoformat(), "qty_sign": 1},
        {"entry_seq": 2, "role": SHORT, "strike": str(STRIKES[SHORT]), "option_type": "PE",
         "expiry": NOV.isoformat(), "qty_sign": -1},
    ]  # fmt: skip
    detail = {
        "underlying": "NIFTY", "entry_session": ENTRY.isoformat(), "expiry": NOV.isoformat(),
        "expiry_kind": "weekly", "direction": "UP", "direction_daily": "UP", "level": str(LEVEL),
        "short_strike": str(STRIKES[SHORT]), "wing_strike": str(STRIKES[WING]),
        "option_type": "PE", "legs": legs, "lot_size": LOT,
    }  # fmt: skip
    day = trade_date or before(ENTRY, 1)
    w.conn.execute(
        "INSERT INTO fo_scan (user_id, sleeve, trade_date, symbol, state, reasons, detail) "
        "VALUES (?, 'F3N', ?, 'NIFTY', ?, ?, ?)",
        (w.uid, day, state, ["f3"], X.dumps(detail)),
    )


def open_f3(w: F3World) -> tuple[str, int]:
    f3_scan(w)
    w.quotes.books.update(spread_book("30", "6"))  # credit 24 on mids
    raised = w.tick(at(ENTRY, 9, 20)).raised
    assert raised == [M.entry_plan_id(FoSleeve.F3N, ENTRY, "NIFTY", w.uid)], raised
    out = w.confirm(raised[0], at(ENTRY, 9, 25))
    assert out.outcome == "OPEN", out
    assert out.position_id is not None
    return raised[0], out.position_id


@requires_db
class TestEntry:
    def test_the_0920_plan_is_wing_then_short_priced_on_mids_with_the_level(
        self, w: F3World
    ) -> None:
        f3_scan(w)
        w.quotes.books.update(spread_book("30", "6"))
        assert w.tick(at(ENTRY, 9, 19)).raised == []
        plan_id = w.tick(at(ENTRY, 9, 20)).raised[0]
        assert w.tick(at(ENTRY, 9, 21)).raised == []  # a second raise is a no-op
        plan = w.plan_row(plan_id)
        assert (plan["status"], plan["kind"], plan["structure"], plan["lots"]) == (
            "ISSUED", "ENTRY", "CREDIT_SPREAD", 1)
        assert Decimal(str(plan["credit_points"])) == Decimal("24.00")
        assert Decimal(str(plan["width_points"])) == Decimal(STRIKES[SHORT] - STRIKES[WING])
        assert X._opt_day(plan["hard_exit_date"]) == NOV  # the expiry day, not E-1 (04 §11)
        legs = w.rows("SELECT entry_seq, role, side, quantity FROM fo_leg WHERE plan_id = ? "
                      "ORDER BY entry_seq", plan["id"])  # fmt: skip
        assert [(lg["role"], lg["side"], lg["quantity"]) for lg in legs] == [
            (WING, "BUY", LOT), (SHORT, "SELL", LOT)]
        detail = X._json(plan["detail"])
        assert Decimal(str(detail["level"])) == LEVEL and detail["direction"] == "UP"
        assert Decimal(str(detail["decay_target_mark"])) == Decimal("4.80")
        assert Decimal(str(detail["loss_cut_mark"])) == Decimal("48.00")
        assert w.fills(plan_id) == []  # raised, never sent

    def test_the_intraday_check_and_a_broken_level_refuse_by_name(self, w: F3World) -> None:
        f3_scan(w)
        w.quotes.books.update(spread_book("30", "6"))
        w.index = M.IndexQuote(Decimal("24150"), Decimal("24200"))  # below the open: not UP
        plan_id = w.tick(at(ENTRY, 9, 20)).raised[0]
        plan = w.plan_row(plan_id)
        assert plan["status"] == "REJECTED_STRUCTURE" and "intraday check" in plan["reason"]
        assert w.rows("SELECT count(*) AS n FROM fo_leg WHERE plan_id = ?", plan["id"])[0]["n"] == 0

    def test_a_level_already_broken_is_no_entry(self, w: F3World) -> None:
        f3_scan(w)
        w.quotes.books.update(spread_book("30", "6"))
        w.index = M.IndexQuote(Decimal("23900"), Decimal("24200"))
        plan = w.plan_row(w.tick(at(ENTRY, 9, 20)).raised[0])
        assert plan["status"] == "REJECTED_STRUCTURE" and "already broken" in plan["reason"]

    def test_no_index_quote_is_a_refusal_not_a_guess(self, w: F3World) -> None:
        f3_scan(w)
        w.quotes.books.update(spread_book("30", "6"))
        w.index = None
        plan = w.plan_row(w.tick(at(ENTRY, 9, 20)).raised[0])
        assert plan["status"] == "REJECTED_STRUCTURE" and "no live quote" in plan["reason"]

    def test_the_confirm_fills_the_wing_first_and_opens_the_spread_with_its_level(
        self, w: F3World
    ) -> None:
        plan_id, pid = open_f3(w)
        assert [f["role"] for f in w.fills(plan_id)] == [WING, SHORT]
        pos = w.position(pid)
        assert (pos["structure"], pos["lots"], pos["lot_size"]) == ("CREDIT_SPREAD", 1, LOT)
        assert Decimal(str(pos["stop_price"])) == LEVEL
        # filled at the touch: the short sold at its bid 29.50, the wing bought at its ask 6.25
        assert Decimal(str(pos["entry_credit"])) == Decimal("23.25")
        assert Decimal(str(pos["profit_take_points"])) == Decimal("4.65")  # 20 % of 23.25
        assert Decimal(str(pos["loss_close_points"])) == Decimal("46.50")  # 2 x 23.25
        assert X._json(pos["legs"])["carry"]["direction"] == "UP"
        assert w.kc.calls == []


@requires_db
class TestExit:
    def test_a_level_break_with_the_flag_off_is_raised_for_the_click_not_sent(
        self, w: F3World
    ) -> None:
        plan_id, pid = open_f3(w)
        day = before(NOV, 5)
        w.index = M.IndexQuote(Decimal("24100"), Decimal("24200"))
        assert w.tick(at(day, 11, 0)).actions == []  # above the level: nothing due
        w.index = M.IndexQuote(Decimal("23970"), Decimal("24200"))  # beyond 24,000 by > 0.10 %
        report = w.tick(at(day, 11, 1))
        assert report.actions == [f"{plan_id}-X"]
        exit_plan = w.plan_row(f"{plan_id}-X")
        assert (exit_plan["kind"], exit_plan["status"]) == ("EXIT", "ISSUED")
        assert X._json(exit_plan["detail"])["code"] == "LEVEL_BREAK"
        assert X._json(exit_plan["detail"])["awaits_click"] is True
        assert w.fills(f"{plan_id}-X") == [] and report.done == []
        assert w.position(pid)["closed_at"] is None
        assert any("waits for the click" in a for a in w.alerts)
        assert w.tick(at(day, 11, 2)).actions == [], w.rows(  # raised once
            "SELECT plan_id, kind, status FROM fo_plan WHERE user_id = ? ORDER BY id", w.uid)

    def test_the_click_sends_the_held_exit_short_first(self, w: F3World) -> None:
        plan_id, pid = open_f3(w)
        day = before(NOV, 5)
        w.index = M.IndexQuote(Decimal("23970"), Decimal("24200"))
        w.tick(at(day, 11, 1))
        out = w.click_exit(f"{plan_id}-X", at(day, 11, 3))
        assert out.outcome == "CLOSED"
        assert [f["role"] for f in w.fills(f"{plan_id}-X")] == [SHORT, WING]
        pos = w.position(pid)
        assert pos["closed_at"] is not None and pos["closed_reason"] == "LEVEL_BREAK"
        assert w.plan_row(plan_id)["status"] == "CLOSED"
        assert w.kc.calls == []

    def test_with_the_flag_on_the_monitor_sends_the_level_break_itself(self, w: F3World) -> None:
        plan_id, pid = open_f3(w)
        w.auto_exit = True
        day = before(NOV, 5)
        w.index = M.IndexQuote(Decimal("23970"), Decimal("24200"))
        report = w.tick(at(day, 11, 1))
        assert report.actions == [f"{plan_id}-X"] and [o.outcome for o in report.done] == ["CLOSED"]
        exit_plan = w.plan_row(f"{plan_id}-X")
        assert exit_plan["status"] == "CLOSED" and exit_plan["confirmed_at"] is not None
        assert [f["role"] for f in w.fills(f"{plan_id}-X")] == [SHORT, WING]
        assert w.position(pid)["closed_reason"] == "LEVEL_BREAK"
        assert w.kc.calls == []

    def test_the_loss_cut_at_twice_the_credit(self, w: F3World) -> None:
        plan_id, pid = open_f3(w)
        w.auto_exit = True
        day = before(NOV, 6)
        w.quotes.books.update(spread_book("48", "3"))  # mark 45 < 46.50
        assert w.tick(at(day, 13, 0)).actions == []
        w.quotes.books.update(spread_book("52", "4"))  # mark 48 >= 46.50
        assert w.tick(at(day, 13, 1)).actions == [f"{plan_id}-X"]
        assert w.position(pid)["closed_reason"] == "LOSS_CUT"

    def test_the_decay_target_at_eighty_percent(self, w: F3World) -> None:
        plan_id, pid = open_f3(w)
        w.auto_exit = True
        day = before(NOV, 4)
        w.quotes.books.update(spread_book("6", "1"))  # mark 5 > 4.65
        assert w.tick(at(day, 14, 0)).actions == []
        w.quotes.books.update(spread_book("5.5", "1"))  # mark 4.5 <= 4.65
        assert w.tick(at(day, 14, 1)).actions == [f"{plan_id}-X"]
        assert w.position(pid)["closed_reason"] == "DECAY_TARGET"

    def test_the_hard_exit_on_expiry_day_at_1500(self, w: F3World) -> None:
        plan_id, pid = open_f3(w)
        w.auto_exit = True
        assert w.tick(at(HARD_EXIT, 15, 0)).actions == []  # E-1 is not F3's day
        assert w.tick(at(NOV, 14, 59)).actions == []
        assert w.tick(at(NOV, 15, 0)).actions == [f"{plan_id}-X"]
        assert w.position(pid)["closed_reason"] == "HARD_EXIT"

    def test_the_click_refuses_a_non_exit_and_a_second_click(self, w: F3World) -> None:
        plan_id, _pid = open_f3(w)
        with pytest.raises(X.Refused) as one:
            w.click_exit(plan_id, at(ENTRY, 9, 30))
        assert one.value.code == "NOT_AN_EXIT"
        day = before(NOV, 5)
        w.index = M.IndexQuote(Decimal("23970"), Decimal("24200"))
        w.tick(at(day, 11, 1))
        w.click_exit(f"{plan_id}-X", at(day, 11, 3))
        with pytest.raises(X.Refused) as two:
            w.click_exit(f"{plan_id}-X", at(day, 11, 4))
        assert two.value.code == "NOT_ISSUED"


@requires_db
class TestAdd:
    def test_the_add_is_raised_the_next_session_only_while_working_and_confirmed_by_click(
        self, w: F3World
    ) -> None:
        plan_id, pid = open_f3(w)
        nxt = before(NOV, 11)
        f3_scan(w, state="OPEN_POSITION", trade_date=ENTRY)  # last night's read: still UP
        w.quotes.books.update(spread_book("20", "3"))  # mark 17 <= 18.60: working
        assert w.tick(at(ENTRY, 9, 30)).raised == [], "not on the entry session"
        raised = w.tick(at(nxt, 9, 20)).raised
        assert raised == [f"{plan_id}-A1"]
        add = w.plan_row(f"{plan_id}-A1")
        assert (add["kind"], add["status"], add["lots"], add["parent_plan_id"]) == (
            "ADD", "ISSUED", 1, plan_id)
        assert w.fills(f"{plan_id}-A1") == []  # a click, never the monitor's
        legs = w.rows("SELECT role, side, quantity FROM fo_leg WHERE plan_id = ? ORDER BY entry_seq",
                      add["id"])  # fmt: skip
        assert [(lg["role"], lg["side"]) for lg in legs] == [(WING, "BUY"), (SHORT, "SELL")]
        out = w.confirm(f"{plan_id}-A1", at(nxt, 9, 22))
        assert out.outcome == "OPEN" and out.position_id == pid
        pos = w.position(pid)
        assert pos["lots"] == 2
        assert Decimal(str(pos["entry_credit"])) == Decimal("19.75")  # (23.25 + 16.25) / 2, at the touch
        assert X._json(pos["legs"])["carry"]["added"] is True
        assert {lg["quantity"] for lg in X._json(pos["legs"])["legs"]} == {2 * LOT}
        assert w.tick(at(nxt, 9, 25)).raised == [], "one add per position"
        assert w.kc.calls == []

    def test_no_add_while_the_trade_is_not_working(self, w: F3World) -> None:
        plan_id, _pid = open_f3(w)
        f3_scan(w, state="OPEN_POSITION", trade_date=ENTRY)
        w.quotes.books.update(spread_book("24", "4"))  # mark 20 > 18.60
        assert w.tick(at(before(NOV, 11), 9, 20)).raised == []
        assert w.store.plan(f"{plan_id}-A1") is None


@requires_db
class TestNightAndPage:
    def test_the_nightly_mark_is_the_settles_spread(self, w: F3World) -> None:
        _plan_id, pid = open_f3(w)
        w.market.landed_days.add(ENTRY)
        w.market.day_prints.setdefault(ENTRY, {}).update({
            ("NIFTY", NOV, Decimal(STRIKES[SHORT]), "PE"): M.Print(Decimal("20"), Decimal("20")),
            ("NIFTY", NOV, Decimal(STRIKES[WING]), "PE"): M.Print(Decimal("5"), Decimal("5")),
        })  # fmt: skip
        report = w.night(ENTRY)
        assert report.marked == 1
        mark = w.rows("SELECT mark_points, pnl_inr FROM fo_mark WHERE position_id = ?", pid)[0]
        assert Decimal(str(mark["mark_points"])) == Decimal("15.00")
        assert Decimal(str(mark["pnl_inr"])) == Decimal("536.25")  # (23.25 - 15) x 65

    def test_the_page_shows_the_spread_the_held_exit_and_the_flag(
        self, w: F3World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan_id, pid = open_f3(w)
        day = before(NOV, 5)
        w.index = M.IndexQuote(Decimal("23970"), Decimal("24200"))
        w.tick(at(day, 11, 1))
        monkeypatch.setattr(DESK, "mode_of", lambda _s: G.fno_gates(FoSleeve.F3N).mode)
        monkeypatch.setattr(DESK, "_now", lambda: at(day, 11, 2))
        view = DESK.build_view(w.store, now=at(day, 11, 2), quotes=w.quotes,
                               spot=lambda _s: Decimal("23970"))  # fmt: skip
        assert view["f3_auto_exit"] is False
        spread = view["spreads"][0]
        assert (spread["id"], spread["direction"], spread["level"]) == (pid, "UP", "24000.00")
        assert spread["mark"] == "24.00" and spread["decay_target"] == "4.65"
        assert "out at once if NIFTY trades below 24000.00" in spread["next_rule"]
        assert any(a["plan_id"] == f"{plan_id}-X" for a in view["actions"]), view["actions"]
        held = next(a for a in view["actions"] if a["plan_id"] == f"{plan_id}-X")
        assert held["confirmable"] is True and held["awaits_click"] is True
        assert "short first" in held["sentence"]
        # the plan card lives on its own day's page
        entry_view = DESK.build_view(w.store, now=at(ENTRY, 9, 30), quotes=w.quotes)
        plan = next(p for p in entry_view["plans"] if p["plan_id"] == plan_id)
        assert plan["group"] == "F3" and "BASKFY_FNO_F3_AUTO_EXIT" in plan["sentence"]
        assert Decimal(plan["level"]) == LEVEL and plan["direction"] == "UP"
        assert "F3N" in view["modes"] and view["modes"]["F3N"] == "PAPER"


class TestTheFlagsAndTheScan:
    def test_the_flags_default_false_and_the_monitor_source_names_no_auto_flag(self) -> None:
        assert G.f3_auto_exit_enabled() is False
        assert G.fno_gates(FoSleeve.F3N).mode.value == "PAPER"
        import inspect
        import re

        for module in (M, X):
            assert not re.search(r"FNO_\w*AUTO", inspect.getsource(module)), module.__name__
