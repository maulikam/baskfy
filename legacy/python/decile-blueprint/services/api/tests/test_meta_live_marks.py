"""``/meta/live-marks`` carries each quote's exchange time and a coverage count — LV1 (27 Sep 2026).

The screens' overlay was honest about *whether* a session existed and silent about *how old* a
print was and *how many* rows it actually marked. Now every quote says when the exchange printed
it and whether that is more than ``stale_after_seconds`` (120) before ``served_at``; the answer
says how many names were asked for and how many got a quote. A partial answer is still live —
the client keeps the close on the rows without a quote and counts them in the status line.

No database: the route is driven directly with its reads monkeypatched (``latest_published_date``,
``_session_day_and_market_open``, ``quotes_permitted``, ``live_quote_details``), and the pure
composer ``_marks_out`` is tested on its own. The wire shape is asserted through the response
model's own dump, so what is checked is what the client receives.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.auth import Principal
from baskfy_api.live_prices import LiveQuote
from baskfy_api.routers import meta
from baskfy_api.schemas import LiveMarksOut
from baskfy_core.market_hours_cb import IST

NOW = dt.datetime(2026, 9, 28, 13, 4, 11, tzinfo=IST)
PUBLISHED = dt.date(2026, 9, 25)


def _quote(seconds_before_now: int | None, *, last: str = "101") -> LiveQuote:
    as_of = None if seconds_before_now is None else NOW - dt.timedelta(seconds=seconds_before_now)
    return LiveQuote(Decimal(last), Decimal("100"), as_of=as_of)


class TestMarksOut:
    """The pure composer: ``details`` from Kite, ``names`` the page asked for, one ``now``."""

    def test_every_quote_carries_its_exchange_time_and_the_answer_its_served_at(self) -> None:
        out = meta._marks_out(
            {"RELIANCE": _quote(30)}, ["RELIANCE"], NOW, as_of=PUBLISHED, market_open=True
        )
        assert out.live is True
        assert out.served_at == NOW
        assert out.quotes["RELIANCE"].as_of == NOW - dt.timedelta(seconds=30)
        assert out.quotes["RELIANCE"].stale is False
        assert out.stale_after_seconds == 120
        assert out.as_of == PUBLISHED

    def test_a_print_older_than_120_seconds_is_stale_and_one_at_120_is_not(self) -> None:
        out = meta._marks_out(
            {"OLD": _quote(121), "EDGE": _quote(120), "FRESH": _quote(1)},
            ["OLD", "EDGE", "FRESH"],
            NOW,
            as_of=PUBLISHED,
            market_open=True,
        )
        assert out.quotes["OLD"].stale is True
        assert out.quotes["EDGE"].stale is False
        assert out.quotes["FRESH"].stale is False
        # A stale row is still served — muted by the client, never dropped into a silent close.
        assert out.marks["OLD"] == Decimal("101")

    def test_a_quote_without_a_stamp_is_not_stale_and_says_so_by_a_null_as_of(self) -> None:
        """Its age is unknown, not zero: ``as_of`` is null so the client shows the gap."""
        out = meta._marks_out({"X": _quote(None)}, ["X"], NOW, as_of=PUBLISHED, market_open=True)
        assert out.quotes["X"].as_of is None
        assert out.quotes["X"].stale is False

    def test_a_stamp_in_the_future_is_not_stale(self) -> None:
        """A clock ahead of ours is not an old print."""
        ahead = LiveQuote(Decimal("1"), None, as_of=NOW + dt.timedelta(seconds=300))
        out = meta._marks_out({"X": ahead}, ["X"], NOW, as_of=PUBLISHED, market_open=True)
        assert out.quotes["X"].stale is False

    def test_a_naive_stamp_is_read_as_ist(self) -> None:
        """Kite's clock is IST; a stamp that lost its zone must not be read as UTC (+5:30 off)."""
        naive = LiveQuote(Decimal("1"), None, as_of=NOW.replace(tzinfo=None))
        out = meta._marks_out({"X": naive}, ["X"], NOW, as_of=PUBLISHED, market_open=True)
        assert out.quotes["X"].stale is False
        old_naive = LiveQuote(
            Decimal("1"), None, as_of=(NOW - dt.timedelta(seconds=200)).replace(tzinfo=None)
        )
        out = meta._marks_out({"X": old_naive}, ["X"], NOW, as_of=PUBLISHED, market_open=True)
        assert out.quotes["X"].stale is True

    def test_requested_and_covered_count_the_page_and_a_partial_answer_is_still_live(
        self,
    ) -> None:
        names = ["RELIANCE", "TCS", "INFY", "NOQUOTE"]
        out = meta._marks_out(
            {"RELIANCE": _quote(1), "TCS": _quote(2), "INFY": _quote(3)},
            names,
            NOW,
            as_of=PUBLISHED,
            market_open=True,
        )
        assert out.live is True
        assert out.reason is None
        assert out.requested == 4
        assert out.covered == 3
        assert "NOQUOTE" not in out.quotes
        assert "NOQUOTE" not in out.marks

    def test_a_session_that_answers_nothing_is_unavailable_with_zero_covered(self) -> None:
        out = meta._marks_out({}, ["RELIANCE", "TCS"], NOW, as_of=PUBLISHED, market_open=True)
        assert out.live is False
        assert out.reason == "unavailable"
        assert out.requested == 2
        assert out.covered == 0
        assert out.served_at == NOW
        assert out.quotes == {}

    def test_covered_never_exceeds_requested(self) -> None:
        """Only names the page asked for count, even if the memo hands back more."""
        out = meta._marks_out(
            {"RELIANCE": _quote(1), "EXTRA": _quote(1)},
            ["RELIANCE"],
            NOW,
            as_of=PUBLISHED,
            market_open=True,
        )
        assert out.requested == 1
        assert out.covered == 1
        assert out.covered <= out.requested

    def test_the_change_is_still_rounded_at_write_time(self) -> None:
        out = meta._marks_out(
            {"R": LiveQuote(Decimal("1520.40"), Decimal("1500.00"), as_of=NOW)},
            ["R"],
            NOW,
            as_of=PUBLISHED,
            market_open=True,
        )
        assert out.quotes["R"].change_pct == Decimal("1.36")


class TestRequestedNames:
    def test_names_are_trimmed_upper_cased_and_distinct(self) -> None:
        assert meta._requested_names(" reliance, TCS ,,Reliance, ") == ["RELIANCE", "TCS"]

    def test_an_empty_query_asks_for_nothing(self) -> None:
        assert meta._requested_names("") == []


class TestTheRoute:
    """The handler with its reads stubbed: which branch answers what, on the wire."""

    @staticmethod
    async def _call(
        monkeypatch: pytest.MonkeyPatch,
        *,
        market_open: bool,
        permitted: bool,
        details: dict[str, LiveQuote] | None,
        symbols: str = "RELIANCE,TCS",
    ) -> dict[str, object]:
        monkeypatch.setattr(meta, "_now", lambda: NOW)

        async def _published(_session: AsyncSession) -> dt.date | None:
            return PUBLISHED

        async def _open(_session: AsyncSession) -> tuple[bool, bool]:
            return True, market_open

        def _details(_symbols: tuple[str, ...]) -> dict[str, LiveQuote]:
            if details is None:
                raise AssertionError("Kite must not be asked for a quote here")
            return details

        monkeypatch.setattr(meta, "latest_published_date", _published)
        monkeypatch.setattr(meta, "_session_day_and_market_open", _open)
        monkeypatch.setattr(meta, "quotes_permitted", lambda: permitted)
        monkeypatch.setattr(meta, "live_quote_details", _details)
        out = await meta.get_live_marks(
            cast(Principal, object()), cast(AsyncSession, object()), symbols
        )
        assert isinstance(out, LiveMarksOut)
        dumped: dict[str, object] = out.model_dump(mode="json")
        return dumped

    @pytest.mark.asyncio
    async def test_a_live_answer_on_the_wire(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = await self._call(
            monkeypatch,
            market_open=True,
            permitted=True,
            details={"RELIANCE": _quote(130, last="1520.40"), "TCS": _quote(5, last="3000")},
        )
        assert body["live"] is True
        assert body["served_at"] == NOW.isoformat()
        assert body["requested"] == 2
        assert body["covered"] == 2
        assert body["stale_after_seconds"] == 120
        quotes = body["quotes"]
        assert isinstance(quotes, dict)
        assert quotes["RELIANCE"] == {
            "last_price": "1520.40",
            "prev_close": "100",
            "change_pct": "1420.40",
            "as_of": (NOW - dt.timedelta(seconds=130)).isoformat(),
            "stale": True,
        }
        assert quotes["TCS"]["stale"] is False
        assert body["as_of"] == PUBLISHED.isoformat()

    @pytest.mark.asyncio
    async def test_a_partial_answer_is_live_with_coverage_short(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = await self._call(
            monkeypatch,
            market_open=True,
            permitted=True,
            details={"RELIANCE": _quote(1)},
            symbols="reliance, TCS, INFY",
        )
        assert body["live"] is True
        assert body["requested"] == 3
        assert body["covered"] == 1

    @pytest.mark.asyncio
    async def test_a_closed_market_counts_the_request_and_never_asks_kite(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = await self._call(monkeypatch, market_open=False, permitted=True, details=None)
        assert body["live"] is False
        assert body["reason"] == "market_closed"
        assert body["requested"] == 2
        assert body["covered"] == 0
        assert body["served_at"] == NOW.isoformat()

    @pytest.mark.asyncio
    async def test_no_session_is_named_and_never_asks_kite(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = await self._call(monkeypatch, market_open=True, permitted=False, details=None)
        assert body["live"] is False
        assert body["reason"] == "no_session"
        assert body["covered"] == 0

    @pytest.mark.asyncio
    async def test_nothing_answered_is_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = await self._call(monkeypatch, market_open=True, permitted=True, details={})
        assert body["live"] is False
        assert body["reason"] == "unavailable"
        assert body["requested"] == 2
        assert body["covered"] == 0
