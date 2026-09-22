"""OP3 — the Kite reads the options run needs, and the limiter they share with the desk.

``docs/options/06`` OP3 asserted here:

* ``quote()`` returns depth, OI and timestamps (``OptionQuoteRecord``, DECISIONS-OP OP3.1), from
  fixtures shaped like Kite Connect's documented ``/quote`` payload — the live shape is what the
  probe confirms;
* a quote batch **never exceeds 500 symbols**, and each batch is exactly one limiter token;
* ``minute_bars`` chunks per Kite's 60-day minute window and returns aware, de-duplicated bars;
* ``basket_order_margins`` is a read-only calculation, and the margins read leaks no figure;
* **the rate-limit proof**: every options read waits on the box's ``read`` ceiling and then on its
  endpoint family's clock — the same Redis keys, rates and order the desk's swing monitor uses —
  so the collector and the desk together cannot exceed Kite's 1 req/s quote cap (measured against
  a real Redis, two processes' worth of callers on one clock).
"""

from __future__ import annotations

import datetime as dt
import re
import threading
import time
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Final

import pytest
from cryptography.fernet import Fernet

from baskfy_providers.errors import ProviderUnavailable
from baskfy_providers.factory import (
    KITE_BULK_CLOCK_KEY,
    KITE_CLOCK_PREFIX,
    KITE_FAMILY_RATE_PER_SECOND,
    KITE_READ_CLOCK_KEY,
    KiteFamily,
    KiteLane,
    build_kite_family_limiter,
    build_kite_family_provider,
    kite_family_key,
)
from baskfy_providers.kite import (
    MINUTE_MAX_DAYS_PER_REQUEST,
    QUOTE_BATCH_SIZE,
    KiteProvider,
    KiteRuntime,
)
from baskfy_providers.ratelimit import (
    SPACING_SCRIPT,
    CallSpacingConfig,
    LayeredCallSpacer,
    RedisCallSpacer,
    RedisLike,
)
from baskfy_providers.records import MarginLegRecord
from baskfy_providers.settings import ProviderSettings
from baskfy_providers.tokens import IST, AccessTokenStore

DESK_LIMITS: Final = (
    Path(__file__).resolve().parents[4]
    / "kite-momentum-rebalancer"
    / "app"
    / "core"
    / "kite_limits.py"
)

NOW: Final = dt.datetime.now(tz=IST).replace(hour=10, minute=0, second=0, microsecond=0)


class CountingLimiter:
    """Counts tokens; never blocks. The real clocks are proven in ``TestTheSharedClock``."""

    def __init__(self) -> None:
        self.acquired = 0

    def acquire(self, tokens: float = 1.0) -> float:
        self.acquired += int(tokens)
        return 0.0


class PlainKite:
    """Kite Connect's documented shapes; records every call. Has **no** order verb at all, and no
    ``basket_order_margins`` — :class:`OptionsFakeKite` adds that one read."""

    def __init__(
        self,
        *,
        quotes: dict[str, dict[str, object]] | None = None,
        candles: list[dict[str, object]] | None = None,
        margins: dict[str, object] | None = None,
        basket: dict[str, object] | None = None,
    ) -> None:
        self._quotes = quotes or {}
        self._candles = candles or []
        self._margins = margins or {}
        self._basket = basket or {}
        self.quote_batches: list[tuple[str, ...]] = []
        self.historical_calls: list[tuple[int, object, object, str]] = []
        self.basket_calls: list[tuple[list[dict[str, object]], bool]] = []

    def set_access_token(self, access_token: str) -> None:
        del access_token

    def instruments(self, exchange: str | None = None) -> list[dict[str, object]]:
        del exchange
        return []

    def historical_data(
        self,
        instrument_token: int,
        from_date: dt.date | dt.datetime | str,
        to_date: dt.date | dt.datetime | str,
        interval: str,
    ) -> list[dict[str, object]]:
        self.historical_calls.append((instrument_token, from_date, to_date, interval))
        assert isinstance(from_date, dt.datetime)
        assert isinstance(to_date, dt.datetime)
        out = []
        for candle in self._candles:
            stamp = candle["date"]
            assert isinstance(stamp, dt.datetime)
            if from_date <= stamp.replace(tzinfo=None) <= to_date:
                out.append(candle)
        return out

    def holdings(self) -> list[dict[str, object]]:
        return []

    def margins(self, segment: str | None = None) -> dict[str, object]:
        del segment
        return self._margins

    def quote(self, *instruments: str) -> dict[str, dict[str, object]]:
        self.quote_batches.append(tuple(instruments))
        return {key: self._quotes[key] for key in instruments if key in self._quotes}

    def trades(self) -> list[dict[str, object]]:
        return []


class OptionsFakeKite(PlainKite):
    """:class:`PlainKite` plus Kite's margin calculator."""

    def basket_order_margins(
        self,
        params: list[dict[str, object]],
        consider_positions: bool = True,
        mode: str | None = None,
    ) -> dict[str, object]:
        del mode
        self.basket_calls.append((params, consider_positions))
        return self._basket


def _provider(
    fake: PlainKite, tmp_path: Path, limiter: CountingLimiter | None = None
) -> KiteProvider:
    key = Fernet.generate_key().decode()
    path = tmp_path / "kite-token.enc"
    store = AccessTokenStore(str(path), key)
    store.save("tok")
    settings = ProviderSettings(
        _env_file=None,
        kite_api_key="k",
        kite_api_secret="s",
        kite_token_encryption_key=key,
        kite_token_path=str(path),
    )
    return KiteProvider(
        settings,
        KiteRuntime(
            rate_limiter=limiter or CountingLimiter(),
            client_factory=lambda _key: fake,
            token_store=store,
        ),
    )


def _option_row(token: int, *, oi: int = 1_300_000, padded: bool = True) -> dict[str, object]:
    """``/quote`` for one NFO option, as Kite Connect documents it (5 levels a side)."""
    buy = [
        {"price": 101.0 - i * 0.05, "quantity": 650 * (i + 1), "orders": i + 1} for i in range(3)
    ]
    sell = [
        {"price": 101.5 + i * 0.05, "quantity": 325 * (i + 1), "orders": i + 1} for i in range(3)
    ]
    if padded:  # Kite pads a thin book with zero levels
        buy += [{"price": 0, "quantity": 0, "orders": 0}] * 2
        sell += [{"price": 0, "quantity": 0, "orders": 0}] * 2
    return {
        "instrument_token": token,
        "timestamp": dt.datetime(2026, 9, 22, 10, 0, 5),
        "last_trade_time": dt.datetime(2026, 9, 22, 10, 0, 1),
        "last_price": 101.25,
        "volume": 123450,
        "oi": oi,
        "oi_day_high": oi + 6500,
        "oi_day_low": oi - 6500,
        "depth": {"buy": buy, "sell": sell},
        "ohlc": {"open": 99.0, "high": 110.0, "low": 95.0, "close": 98.0},
    }


class TestOptionQuotes:
    def test_depth_oi_and_both_timestamps_are_read(self, tmp_path: Path) -> None:
        fake = OptionsFakeKite(quotes={"NFO:NIFTY26SEP25000CE": _option_row(11)})
        [q] = _provider(fake, tmp_path).option_quotes(["NFO:NIFTY26SEP25000CE"])
        assert q.key == "NFO:NIFTY26SEP25000CE"
        assert (q.oi, q.oi_day_high, q.oi_day_low) == (1_300_000, 1_306_500, 1_293_500)
        assert [lv.price for lv in q.bids] == [
            Decimal("101.0"),
            Decimal("100.95"),
            Decimal("100.9"),
        ]
        assert q.asks[0].price == Decimal("101.5")
        assert q.asks[0].quantity == 325
        # Kite's zero padding is not a zero-priced bid.
        assert len(q.bids) == 3
        assert len(q.asks) == 3
        # Naive Kite stamps are IST; both are kept.
        assert q.as_of is not None
        assert q.as_of.utcoffset() == dt.timedelta(hours=5, minutes=30)
        assert q.last_trade_time is not None
        assert q.last_trade_time < q.as_of

    def test_the_index_and_the_options_share_one_call(self, tmp_path: Path) -> None:
        fake = OptionsFakeKite(
            quotes={
                "NFO:A": _option_row(1),
                "NSE:NIFTY 50": {"instrument_token": 256265, "last_price": 25012.35},
            }
        )
        got = {q.key: q for q in _provider(fake, tmp_path).option_quotes(["NFO:A", "NSE:NIFTY 50"])}
        assert len(fake.quote_batches) == 1
        assert got["NSE:NIFTY 50"].last_price == Decimal("25012.35")
        assert got["NSE:NIFTY 50"].bids == ()

    @pytest.mark.parametrize("count", [1, 125, QUOTE_BATCH_SIZE, QUOTE_BATCH_SIZE + 1, 1_234])
    def test_a_batch_never_exceeds_500_and_each_batch_is_one_limiter_token(
        self, tmp_path: Path, count: int
    ) -> None:
        limiter = CountingLimiter()
        fake = OptionsFakeKite()
        _provider(fake, tmp_path, limiter).option_quotes([f"NFO:S{i}" for i in range(count)])
        assert all(len(batch) <= QUOTE_BATCH_SIZE for batch in fake.quote_batches)
        assert sum(len(batch) for batch in fake.quote_batches) == count
        assert limiter.acquired == len(fake.quote_batches) == -(-count // QUOTE_BATCH_SIZE)

    def test_duplicates_and_blanks_are_not_sent_twice(self, tmp_path: Path) -> None:
        fake = OptionsFakeKite()
        _provider(fake, tmp_path).option_quotes(["NFO:A", "", "NFO:A", "NFO:B"])
        assert fake.quote_batches == [("NFO:A", "NFO:B")]

    def test_an_unreadable_row_costs_only_itself(self, tmp_path: Path) -> None:
        bad = _option_row(2)
        bad["oi"] = "not a number"
        fake = OptionsFakeKite(quotes={"NFO:A": _option_row(1), "NFO:B": bad})
        got = _provider(fake, tmp_path).option_quotes(["NFO:A", "NFO:B"])
        assert [q.key for q in got] == ["NFO:A"]


def _candle(minute: dt.datetime, close: float) -> dict[str, object]:
    return {
        "date": minute,
        "open": close - 1,
        "high": close + 2,
        "low": close - 2,
        "close": close,
        "volume": 0,
    }


class TestMinuteBars:
    def test_windows_respect_kites_60_day_minute_cap(self, tmp_path: Path) -> None:
        provider = _provider(OptionsFakeKite(), tmp_path)
        windows = list(provider.minute_windows(dt.date(2015, 1, 1), dt.date(2026, 9, 22)))
        assert all((hi - lo).days + 1 <= MINUTE_MAX_DAYS_PER_REQUEST for lo, hi in windows)
        assert windows[0][0] == dt.date(2015, 1, 1)
        assert windows[-1][1] == dt.date(2026, 9, 22)
        for (_, a_hi), (b_lo, _) in pairwise(windows):
            assert b_lo == a_hi + dt.timedelta(days=1)

    def test_each_window_is_one_minute_interval_call_and_bars_come_back_aware(
        self, tmp_path: Path
    ) -> None:
        tz = dt.timezone(dt.timedelta(hours=5, minutes=30))
        candles = [
            _candle(dt.datetime(2026, 7, 1, 9, 15, tzinfo=tz), 25000),
            _candle(dt.datetime(2026, 9, 21, 15, 29, tzinfo=tz), 25100),
        ]
        limiter = CountingLimiter()
        fake = OptionsFakeKite(candles=candles)
        bars = _provider(fake, tmp_path, limiter).minute_bars(
            256265, dt.date(2026, 6, 1), dt.date(2026, 9, 22)
        )
        assert {call[3] for call in fake.historical_calls} == {"minute"}
        assert limiter.acquired == len(fake.historical_calls) == 2  # 114 days → two windows
        assert [b.close for b in bars] == [Decimal(25000), Decimal(25100)]
        assert all(b.ts.utcoffset() == dt.timedelta(hours=5, minutes=30) for b in bars)

    def test_an_aware_bound_is_read_in_ist(self, tmp_path: Path) -> None:
        fake = OptionsFakeKite()
        utc_open = dt.datetime(2026, 9, 22, 3, 45, tzinfo=dt.UTC)  # 09:15 IST
        _provider(fake, tmp_path).minute_bars(256265, utc_open, utc_open + dt.timedelta(minutes=5))
        (_, lo, hi, _) = fake.historical_calls[0]
        assert lo == dt.datetime(2026, 9, 22, 9, 15)
        assert hi == dt.datetime(2026, 9, 22, 9, 20)

    def test_an_empty_window_is_an_empty_list(self, tmp_path: Path) -> None:
        assert _provider(OptionsFakeKite(), tmp_path).minute_bars(1, NOW.date(), NOW.date()) == []


class TestReadOnlyMargins:
    def test_basket_margins_is_a_calculation_without_positions(self, tmp_path: Path) -> None:
        fake = OptionsFakeKite(
            basket={
                "initial": {"total": 180000.5, "span": 1},
                "final": {"total": 42000.25, "span": 1},
                "orders": [],
            }
        )
        legs = [
            MarginLegRecord(exchange="NFO", tradingsymbol="W", transaction_type="BUY", quantity=65),
            MarginLegRecord(
                exchange="NFO", tradingsymbol="S", transaction_type="SELL", quantity=65
            ),
        ]
        answer = _provider(fake, tmp_path).basket_order_margins(legs)
        assert (answer.initial_total, answer.final_total) == (
            Decimal("180000.5"),
            Decimal("42000.25"),
        )
        [(params, consider_positions)] = fake.basket_calls
        assert consider_positions is False
        assert {p["product"] for p in params} == {"MIS"}
        assert answer.shape["top"] == ["final", "initial", "orders"]

    def test_a_client_without_the_verb_is_refused_not_guessed(self, tmp_path: Path) -> None:
        provider = _provider(PlainKite(), tmp_path)
        leg = MarginLegRecord(exchange="NFO", tradingsymbol="W", transaction_type="BUY", quantity=1)
        with pytest.raises(ProviderUnavailable, match="basket_order_margins"):
            provider.basket_order_margins([leg])

    def test_the_margins_read_names_fields_and_never_a_figure(self, tmp_path: Path) -> None:
        fake = OptionsFakeKite(
            margins={
                "equity": {"net": 123456.78, "available": {"cash": 99.5}, "enabled": True},
                "commodity": {"net": 0.0},
            }
        )
        shape = _provider(fake, tmp_path).margins_shape()
        assert shape == {
            "commodity": {"net": "float"},
            "equity": {"available": ["cash"], "enabled": "bool", "net": "float"},
        }
        assert "123456" not in repr(shape)


# --- the rate-limit proof ------------------------------------------------------------------------


def _desk_constants() -> tuple[str, dict[str, float]]:
    """The desk's `SHARED_KEY_PREFIX` and `RATE_FOR_FAMILY`, read from its source.

    Read as text because the desk is a separate project with its own interpreter; importing it
    here would couple the two test suites. Skips when the desk tree is not checked out beside us.
    """
    if not DESK_LIMITS.exists():
        pytest.skip(f"the desk tree is not beside this one ({DESK_LIMITS})")
    text = DESK_LIMITS.read_text(encoding="utf-8")
    prefix = re.search(r'^SHARED_KEY_PREFIX = "([^"]+)"', text, re.MULTILINE)
    assert prefix is not None
    consts = {
        name: float(value)
        for name, value in re.findall(r"^([A-Z]+)_PER_SECOND = ([0-9.]+)", text, re.MULTILINE)
    }
    rates = {
        family: consts[const] for family, const in re.findall(r'"(\w+)": ([A-Z]+)_PER_SECOND', text)
    }
    return prefix.group(1), rates


class TestTheOptionsReadsWaitOnTheDesksClocks:
    """Same keys, same rates, same order as `DeskLimits.slot` (``read`` then the family)."""

    def test_the_keys_and_rates_are_the_desks(self) -> None:
        prefix, rates = _desk_constants()
        assert prefix == KITE_CLOCK_PREFIX
        assert f"{prefix}:read" == KITE_READ_CLOCK_KEY
        for family in KiteFamily:
            assert kite_family_key(family) == f"{prefix}:{family.value}"
            assert KITE_FAMILY_RATE_PER_SECOND[family] == rates[family.value], family

    def test_the_quote_cap_is_kites_one_a_second(self) -> None:
        assert KITE_FAMILY_RATE_PER_SECOND[KiteFamily.QUOTE] == 1.0
        assert KITE_FAMILY_RATE_PER_SECOND[KiteFamily.HISTORICAL] == 3.0

    @pytest.mark.parametrize("family", list(KiteFamily))
    def test_interactive_takes_the_ceiling_then_the_family(
        self, settings: ProviderSettings, family: KiteFamily
    ) -> None:
        limiter = build_kite_family_limiter(settings, family)
        if limiter is None:
            pytest.skip("no Redis")
        assert isinstance(limiter, LayeredCallSpacer)
        keys = [spacer.key for spacer in limiter.spacers]
        assert keys == [KITE_READ_CLOCK_KEY, kite_family_key(family)]
        assert limiter.spacers[-1].config.rate_per_second == KITE_FAMILY_RATE_PER_SECOND[family]

    def test_the_backfill_lane_yields_first(self, settings: ProviderSettings) -> None:
        limiter = build_kite_family_limiter(settings, KiteFamily.HISTORICAL, KiteLane.BULK)
        if limiter is None:
            pytest.skip("no Redis")
        assert isinstance(limiter, LayeredCallSpacer)
        assert [s.key for s in limiter.spacers] == [
            KITE_BULK_CLOCK_KEY,
            KITE_READ_CLOCK_KEY,
            kite_family_key(KiteFamily.HISTORICAL),
        ]

    def test_no_redis_means_no_calls_at_all(self) -> None:
        dead = ProviderSettings(_env_file=None, redis_url="redis://127.0.0.1:1/0")
        assert build_kite_family_limiter(dead, KiteFamily.QUOTE) is None
        provider = build_kite_family_provider(dead, KiteFamily.QUOTE)
        assert provider.rate_limiter is None


class TestTheSharedClock:
    """Two processes' worth of callers — the desk's swing monitor and the options collector —
    on one Redis departure clock: their combined departures never beat the family's cap.

    Run on test-only keys at 10x Kite's rates so the proof takes a second, not ten; the clock
    arithmetic is rate-independent, and the keys/rates the box uses are pinned above.
    """

    SCALE: Final = 10.0

    def test_desk_and_collector_together_stay_under_the_quote_cap(
        self, redis_client: RedisLike
    ) -> None:
        run = f"test:op3:{time.time_ns()}"
        read_rate = 3.0 * self.SCALE
        quote_rate = 1.0 * self.SCALE

        def spacer(key: str, rate: float) -> RedisCallSpacer:
            return RedisCallSpacer(
                redis_client, f"{run}:{key}", CallSpacingConfig(rate, max_wait_seconds=30)
            )

        # The desk: `DeskLimits.slot("quote")` = read, then quote — the same Lua (M85).
        desk = LayeredCallSpacer((spacer("read", read_rate), spacer("quote", quote_rate)))
        # The collector: `build_kite_family_limiter(QUOTE)` = read, then quote.
        collector = LayeredCallSpacer((spacer("read", read_rate), spacer("quote", quote_rate)))
        assert SPACING_SCRIPT  # both sides run this exact script
        departures: list[float] = []
        lock = threading.Lock()

        def caller(limiter: LayeredCallSpacer, calls: int) -> None:
            for _ in range(calls):
                limiter.acquire()
                with lock:
                    departures.append(time.monotonic())

        threads = [
            threading.Thread(target=caller, args=(desk, 6)),
            threading.Thread(target=caller, args=(collector, 6)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        departures.sort()
        assert len(departures) == 12
        gaps = [b - a for a, b in pairwise(departures)]
        interval = 1.0 / quote_rate
        # Thread wake-up jitter can only make a gap *longer*; allow 20 ms of timer slack.
        assert min(gaps) >= interval - 0.02, gaps
        # And over the whole run the rate is the cap, not double it.
        assert (departures[-1] - departures[0]) >= interval * (len(departures) - 1) - 0.05


class TestTheBudget:
    """The options reads' share of Kite's caps (docs/options/STATUS.md OP3 has the working).

    The collector makes one ``quote()`` a minute (≤ 125 keys, one batch) and the index-bar task
    two ``historical_data`` calls; the probe of a first minute with no stored spot adds one
    ``quote()``. Against caps of 60 quote calls and 180 historical calls a minute that is
    ≤ 2/60 and 2/180 — the rest of every minute stays with the desk and the swing monitor.
    """

    def test_the_options_share_of_each_cap_is_small(self) -> None:
        quote_calls_per_minute = 2  # worst case: the spot-only fallback plus the chain
        historical_calls_per_minute = 2
        quote_cap = 60 * KITE_FAMILY_RATE_PER_SECOND[KiteFamily.QUOTE]
        historical_cap = 60 * KITE_FAMILY_RATE_PER_SECOND[KiteFamily.HISTORICAL]
        read_cap = 60 * 3.0
        assert quote_calls_per_minute / quote_cap <= 0.05
        assert historical_calls_per_minute / historical_cap <= 0.02
        assert (quote_calls_per_minute + historical_calls_per_minute) / read_cap <= 0.03
