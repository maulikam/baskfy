"""FO7 — the FO monitor's clocks and its Kite seams (``docs/fno/06`` FO7).

* ``scripts.fno_monitor_loop`` (compose-ready; FO12 wires it): starts ``app.fno_monitor`` at 09:14
  on a weekday, at once inside the day's window (which runs to 23:30, because the book is marked
  after the bhavcopy lands), never on a weekend; a crash inside the window restarts it;
* ``app.fno_clock.run_day``: a tick a minute from 09:15 to the close, none before the open, then the
  night retried until the bhavcopy has landed; a failing tick does not stop the day;
* the Kite seams read, and only read: ``kite_quotes`` (depth, last, OI), ``kite_margins``
  (``basket_order_margins`` against free margin, ``None`` when unreadable), and the futures master.
"""
from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal
from typing import Any

from app import fno_clock as K
from app import fno_monitor as M
from scripts import fno_monitor_loop as L

IST = L.IST
WED = dt.date(2026, 10, 28)


def at(day: dt.date, hh: int, mm: int) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hh, mm), tzinfo=IST)


class TestTheLoop:
    def test_it_starts_at_0914_and_at_once_inside_the_window(self) -> None:
        assert L.next_run(at(WED, 8, 0)) == at(WED, 9, 14)
        assert L.next_run(at(WED, 19, 30)) == at(WED, 19, 30)  # the night still owes its marks
        assert L.next_run(at(WED, 23, 45)) == at(WED + dt.timedelta(days=1), 9, 14)
        assert L.next_run(at(dt.date(2026, 10, 31), 10, 0)) == at(dt.date(2026, 11, 2), 9, 14)

    def test_a_crash_in_the_window_restarts_and_a_clean_exit_ends_the_day(self) -> None:
        moments = iter([at(WED, 10, 0)] * 3 + [at(WED, 10, 1)] * 3 + [at(WED, 23, 31)] * 6)
        codes = iter([1, 0])
        calls: list[list[str]] = []
        sleeps: list[float] = []

        def call(argv: list[str]) -> int:
            calls.append(argv)
            return next(codes)

        L.run_forever(now_fn=lambda: next(moments), sleep_fn=sleeps.append, call_fn=call, limit=2)
        assert len(calls) == 2 and calls[0][1:] == ["-m", "app.fno_monitor"]
        assert L.RETRY_SECONDS in sleeps


class FakeClock:
    def __init__(self, start: dt.datetime) -> None:
        self.now = start

    def __call__(self) -> dt.datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += dt.timedelta(seconds=seconds)


class Night:
    def __init__(self, landed: bool) -> None:
        self.landed = landed


class FakeMonitor:
    def __init__(self, land_at: dt.time) -> None:
        self.ticks: list[dt.datetime] = []
        self.nights: list[dt.date] = []
        self.clock: FakeClock | None = None
        self.land_at = land_at

    async def tick(self, now: dt.datetime) -> object:
        self.ticks.append(now)
        if now.time() == dt.time(11, 0):
            raise RuntimeError("fixture: one bad quote")
        return None

    async def nightly(self, day: dt.date) -> Night:
        self.nights.append(day)
        assert self.clock is not None
        return Night(self.clock.now.time() >= self.land_at)


class TestTheDay:
    def test_a_tick_a_minute_in_session_then_the_night_until_it_lands(self) -> None:
        clock = FakeClock(at(WED, 9, 14))
        monitor = FakeMonitor(land_at=dt.time(19, 0))
        monitor.clock = clock
        report = asyncio.run(K.run_day(monitor, now=clock, sleep=clock.sleep))
        assert monitor.ticks[0] == at(WED, 9, 15) and monitor.ticks[-1] == at(WED, 15, 29)
        assert len(monitor.ticks) == 375  # 09:15 to 15:29, one a minute; none before the open
        assert report.errors and "one bad quote" in report.errors[0]  # logged, the day went on
        assert report.marked and monitor.nights[-1] == WED
        assert clock.now.time() < dt.time(19, 16)  # stopped once the night landed

    def test_a_night_that_never_lands_ends_at_2330(self) -> None:
        clock = FakeClock(at(WED, 15, 31))
        monitor = FakeMonitor(land_at=dt.time(23, 59))
        monitor.clock = clock
        report = asyncio.run(K.run_day(monitor, now=clock, sleep=clock.sleep))
        assert not report.marked and monitor.ticks == []
        assert clock.now.time() >= K.NIGHT_END


class FakeKite:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.limits = self

    def slot(self, _family: str) -> None:
        self.calls.append("slot")

    def quote_raw(self, keys: list[str]) -> dict[str, Any]:
        self.calls.append(f"quote_raw:{len(keys)}")
        return {
            "NFO:NIFTY26NOV25000CE": {
                "last_price": 60.4, "oi": 1300000,
                "depth": {"buy": [{"price": 60.0, "quantity": 650}, {"price": 0, "quantity": 0}],
                          "sell": [{"price": 61.0, "quantity": 1300}]},
            },
        }  # fmt: skip

    def margins(self, segment: str) -> dict[str, Any]:
        self.calls.append(f"margins:{segment}")
        return {"net": 500000.0}

    @property
    def kc(self) -> FakeKite:
        return self

    def basket_order_margins(self, orders: list[dict[str, Any]], **_: object) -> dict[str, Any]:
        self.calls.append(f"basket:{len(orders)}:{orders[0]['product']}")
        return {"initial": {"total": 190000.0}, "final": {"total": 61000.0}}

    def instruments(self, exchange: str) -> list[dict[str, Any]]:
        self.calls.append(f"instruments:{exchange}")
        return [
            {"name": "RELIANCE", "instrument_type": "FUT", "tradingsymbol": "RELIANCE26DECFUT",
             "instrument_token": 2, "lot_size": 500, "expiry": "2026-12-29"},
            {"name": "RELIANCE", "instrument_type": "FUT", "tradingsymbol": "RELIANCE26NOVFUT",
             "instrument_token": 1, "lot_size": 500, "expiry": "2026-11-24"},
            {"name": "RELIANCE", "instrument_type": "CE", "tradingsymbol": "RELIANCE26NOV1500CE",
             "instrument_token": 3, "lot_size": 500, "expiry": "2026-11-24"},
        ]  # fmt: skip


class TestTheKiteSeams:
    def test_quotes_carry_depth_last_and_oi(self) -> None:
        kite = FakeKite()
        books = M.kite_quotes(kite)(["NIFTY26NOV25000CE", "NIFTY26NOV25000CE"])
        q = books["NIFTY26NOV25000CE"]
        assert (q.bid, q.ask, q.last, q.oi) == (Decimal("60.0"), Decimal("61.0"),
                                                Decimal("60.4"), 1300000)  # fmt: skip
        assert len(q.bids) == 1 and q.mid == Decimal("60.5")
        assert kite.calls == ["quote_raw:1"]  # one key once

    def test_margins_are_the_baskets_final_total_against_free_margin(self) -> None:
        kite = FakeKite()
        leg = M.MarginLeg("NIFTY26NOV25000CE", "SELL", 65, Decimal("60"))
        quote = M.kite_margins(kite)([leg])
        assert quote == M.MarginQuote(Decimal("61000.0"), Decimal("500000.0"))
        assert "basket:1:NRML" in kite.calls

    def test_an_unreadable_margin_is_none(self) -> None:
        class Broken(FakeKite):
            def basket_order_margins(self, orders: list[dict[str, Any]],
                                     **_: object) -> dict[str, Any]:  # fmt: skip
                raise ConnectionError("fixture: the margin endpoint is down")

        assert M.kite_margins(Broken())([]) is None

    def test_futures_come_from_the_nfo_master_once(self) -> None:
        kite = FakeKite()
        master = M.PgKiteInstruments(conn=None, kite=kite)
        nov = master.future("RELIANCE", dt.date(2026, 11, 24))
        assert nov is not None and nov.tradingsymbol == "RELIANCE26NOVFUT"
        nxt = master.next_future("RELIANCE", dt.date(2026, 11, 24))
        assert nxt is not None and (nxt.tradingsymbol, nxt.lot_size) == ("RELIANCE26DECFUT", 500)
        assert master.next_future("RELIANCE", dt.date(2026, 12, 29)) is None
        assert kite.calls == ["instruments:NFO"]
