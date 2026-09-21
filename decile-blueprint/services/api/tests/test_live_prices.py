"""Live marks for held instruments — M82.

The portfolio valued positions at `ohlcv_daily.close_raw`, the previous session's close. Maulik
asked for the market: on 2 Sep 2026 ATHERENERG was trading at 1692.50 and the page showed the
close. Kite's holdings payload already carries `last_price`, and `HoldingRow` already carried it —
it was simply being discarded.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import cast

import pytest
from baskfy_execution.broker_ports import HoldingRow
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api import live_prices
from baskfy_api.broker_holdings import HoldingsResult, HoldingsSource


def _row(symbol: str, last: str | None) -> HoldingRow:
    return HoldingRow(
        symbol=symbol,
        exchange="NSE",
        quantity=Decimal("10"),
        t1_quantity=Decimal("0"),
        collateral_quantity=Decimal("0"),
        average_price=Decimal("100"),
        last_price=None if last is None else Decimal(last),
    )


@pytest.fixture(autouse=True)
def _clear() -> None:
    live_prices.reset_cache()


class TestOnlyALiveReadIsPriced:
    """The guard that matters: an invented price behind a real rupee total is the worst outcome."""

    @pytest.mark.parametrize("source", ["fixture", "empty", "unwired"])
    def test_a_non_live_source_prices_nothing(
        self, source: HoldingsSource, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = (_row("RELIANCE", "100"),) if source == "fixture" else ()
        monkeypatch.setattr(
            live_prices, "holdings_for_broker", lambda _b: HoldingsResult(rows=rows, source=source)
        )
        assert live_prices.live_prices_by_symbol() == {}

    def test_a_degraded_read_prices_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = HoldingsResult(rows=(_row("RELIANCE", "100"),), source="fixture", degraded=True)
        monkeypatch.setattr(live_prices, "holdings_for_broker", lambda _b: result)
        assert live_prices.live_prices_by_symbol() == {}

    def test_a_broker_that_raises_prices_nothing_rather_than_failing_the_page(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty map leaves the close in place — a correct number with a known meaning.

        This is the one place quiet degradation is right: the alternative is an empty portfolio
        because a quote timed out.
        """

        def _boom(_b: str) -> HoldingsResult:
            raise RuntimeError("kite unreachable")

        monkeypatch.setattr(live_prices, "holdings_for_broker", _boom)
        assert live_prices.live_prices_by_symbol() == {}


class TestALiveReadIsPriced:
    def test_symbols_carry_their_last_price(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = HoldingsResult(
            rows=(_row("ATHERENERG", "1692.5"), _row("CUPID", "278.15")), source="live"
        )
        monkeypatch.setattr(live_prices, "holdings_for_broker", lambda _b: result)
        assert live_prices.live_prices_by_symbol() == {
            "ATHERENERG": Decimal("1692.5"),
            "CUPID": Decimal("278.15"),
        }

    def test_a_missing_or_zero_price_is_dropped_not_zeroed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A zero mark would value a holding at nothing. Absent is not the same as worthless."""
        result = HoldingsResult(
            rows=(_row("NOPRICE", None), _row("ZERO", "0"), _row("GOOD", "10")), source="live"
        )
        monkeypatch.setattr(live_prices, "holdings_for_broker", lambda _b: result)
        assert live_prices.live_prices_by_symbol() == {"GOOD": Decimal("10")}

    def test_the_second_call_is_served_from_the_memo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A page render must not be a broker call, and the overview refetches on every poll."""
        calls: list[str] = []

        def _count(broker: str) -> HoldingsResult:
            calls.append(broker)
            return HoldingsResult(rows=(_row("GOOD", "10"),), source="live")

        monkeypatch.setattr(live_prices, "holdings_for_broker", _count)
        live_prices.live_prices_by_symbol()
        live_prices.live_prices_by_symbol()
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_live_prices_by_instrument_runs_kite_io_via_to_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit 4.11/4.12: sync kiteconnect must not block the async event loop."""
    ran: list[str] = []

    async def _capture(fn: Callable[..., object], *args: object, **kwargs: object) -> object:
        ran.append(getattr(fn, "__name__", "fn"))
        return fn(*args, **kwargs)

    monkeypatch.setattr("baskfy_api.live_prices.anyio.to_thread.run_sync", _capture)
    monkeypatch.setattr(
        live_prices,
        "holdings_for_broker",
        lambda _b: HoldingsResult(rows=(_row("GOOD", "10"),), source="live"),
    )

    class _Row:
        id = 1
        symbol = "GOOD"

    class _Result:
        def all(self) -> list[_Row]:
            return [_Row()]

    class _Session:
        async def execute(self, _stmt: object) -> _Result:
            return _Result()

    prices = await live_prices.live_prices_by_instrument(cast(AsyncSession, _Session()), [1])
    assert prices == {1: Decimal("10")}
    assert ran == ["live_prices_by_symbol"]


class TestQuotesAreRefusedWithoutALiveSession:
    def test_dry_run_is_not_a_live_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DRY_RUN", "true")
        monkeypatch.setenv("BASKFY_KITE_API_KEY", "not-a-secret-for-this-test")
        assert live_prices.quotes_permitted() is False


class TestQuotesFillNamesTheBookOmitted:
    """A Kite login marks every held name, not only the ones on the holdings payload."""

    def test_quotes_are_refused_when_the_session_is_not_live(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(live_prices, "quotes_permitted", lambda: False)
        monkeypatch.setattr(
            live_prices,
            "_quote_symbols",
            lambda _s: (_ for _ in ()).throw(AssertionError("must not quote")),
        )
        assert live_prices.live_quotes_by_symbol(["RELIANCE"]) == {}

    def test_a_permitted_session_quotes_the_missing_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(live_prices, "quotes_permitted", lambda: True)
        monkeypatch.setattr(
            live_prices, "_quote_symbols", lambda symbols: {symbols[0]: Decimal("1692.5")}
        )
        quoted = live_prices.live_quotes_by_symbol(["atherenerg"])
        assert quoted == {"ATHERENERG": Decimal("1692.5")}

    def test_a_quote_that_returns_nothing_leaves_the_close_in_place(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(live_prices, "quotes_permitted", lambda: True)
        monkeypatch.setattr(live_prices, "_quote_symbols", lambda _s: {})
        assert live_prices.live_quotes_by_symbol(["RELIANCE"]) == {}


@pytest.mark.asyncio
async def test_live_prices_by_instrument_quotes_names_the_book_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual / CAS names stay on close unless a live session can quote them."""
    monkeypatch.setattr(
        live_prices,
        "holdings_for_broker",
        lambda _b: HoldingsResult(rows=(_row("GOOD", "10"),), source="live"),
    )
    monkeypatch.setattr(live_prices, "quotes_permitted", lambda: True)
    monkeypatch.setattr(live_prices, "_quote_symbols", lambda symbols: {symbols[0]: Decimal("99")})

    class _Row:
        def __init__(self, instrument_id: int, symbol: str) -> None:
            self.id = instrument_id
            self.symbol = symbol

    class _Result:
        def all(self) -> list[_Row]:
            return [_Row(1, "GOOD"), _Row(2, "CASONLY")]

    class _Session:
        async def execute(self, _stmt: object) -> _Result:
            return _Result()

    prices = await live_prices.live_prices_by_instrument(cast(AsyncSession, _Session()), [1, 2])
    assert prices == {1: Decimal("10"), 2: Decimal("99")}


@pytest.mark.asyncio
async def test_live_marks_for_symbols_uses_the_book_then_quotes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        live_prices,
        "holdings_for_broker",
        lambda _b: HoldingsResult(rows=(_row("GOOD", "10"),), source="live"),
    )
    monkeypatch.setattr(live_prices, "quotes_permitted", lambda: True)
    monkeypatch.setattr(live_prices, "_quote_symbols", lambda symbols: {symbols[0]: Decimal("99")})
    marks = await live_prices.live_marks_for_symbols(["good", "CASONLY", "good"])
    assert marks == {"GOOD": Decimal("10"), "CASONLY": Decimal("99")}


@pytest.mark.asyncio
async def test_live_marks_for_symbols_is_empty_without_names() -> None:
    assert await live_prices.live_marks_for_symbols([]) == {}
    assert await live_prices.live_marks_for_symbols(["", "  "]) == {}


@pytest.mark.asyncio
async def test_live_marks_for_symbols_caps_the_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    book = {f"S{i}": Decimal("1") for i in range(600)}
    monkeypatch.setattr(live_prices, "live_prices_by_symbol", lambda: book)
    monkeypatch.setattr(live_prices, "live_quotes_by_symbol", lambda _symbols: {})
    marks = await live_prices.live_marks_for_symbols([f"S{i}" for i in range(600)])
    assert len(marks) == live_prices.MAX_LIVE_MARKS
    assert "S0" in marks
    assert "S499" in marks
    assert "S500" not in marks


class TestScreenQuoteDetails:
    """The screens' overlay (21 Sep 2026): last price plus Kite's own previous close."""

    def test_refused_without_a_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(live_prices, "quotes_permitted", lambda: False)

        def _boom(_symbols: object) -> dict[str, live_prices.LiveQuote]:
            raise AssertionError("no quote without a session")

        monkeypatch.setattr(live_prices, "_quote_details", _boom)
        assert live_prices.live_quote_details(["RELIANCE"]) == {}

    def test_keeps_prev_close_and_caches_the_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[str, ...]] = []

        def _fetch(symbols: tuple[str, ...] | list[str]) -> dict[str, live_prices.LiveQuote]:
            calls.append(tuple(symbols))
            return {s: live_prices.LiveQuote(Decimal("101"), Decimal("100")) for s in symbols}

        monkeypatch.setattr(live_prices, "quotes_permitted", lambda: True)
        monkeypatch.setattr(live_prices, "_quote_details", _fetch)
        first = live_prices.live_quote_details(["reliance", "TCS", "RELIANCE"])
        assert first == {
            "RELIANCE": live_prices.LiveQuote(Decimal("101"), Decimal("100")),
            "TCS": live_prices.LiveQuote(Decimal("101"), Decimal("100")),
        }
        live_prices.live_quote_details(["RELIANCE", "TCS"])
        assert calls == [("RELIANCE", "TCS")]

    def test_caps_a_request_at_one_kite_batch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[int] = []

        def _fetch(symbols: list[str]) -> dict[str, live_prices.LiveQuote]:
            seen.append(len(symbols))
            return {}

        monkeypatch.setattr(live_prices, "quotes_permitted", lambda: True)
        monkeypatch.setattr(live_prices, "_quote_details", _fetch)
        live_prices.live_quote_details([f"S{i}" for i in range(900)])
        assert seen == [live_prices.MAX_LIVE_MARKS]
