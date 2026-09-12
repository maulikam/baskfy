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
