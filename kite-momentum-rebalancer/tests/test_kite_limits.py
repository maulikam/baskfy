"""SW20: no Kite read leaves this process without a slot, and each family has Kite's own cap.

The order path has been limited since the gateway existed. The reads were not: twenty-six calls
in `app/kite_client.py` and twenty-five more made directly on the client from `app/analytics/`,
none of them waiting for anybody. This file is what stops that coming back — the burst tests
prove the caps, and the two source scans fail the moment somebody adds a call that skips them.

The scans are the part worth keeping honest. A rate limiter is only as good as the narrowest
path around it, and on this desk the path around it was `k.kc.<anything>`.
"""
from __future__ import annotations

import ast
import inspect
import threading
from pathlib import Path

import pytest

from app import kite_client as KC
from app.core.kite_limits import (
    FAMILY_FOR_CALL,
    GENERAL_PER_SECOND,
    HISTORICAL_PER_SECOND,
    QUOTE_PER_SECOND,
    DeskLimits,
    Spacer,
)

APP = Path(__file__).resolve().parents[1] / "app"

#: `app/analytics/` modules that may still hold the raw client, with the reason. Every entry is
#: a call the limited wrappers do not cover; an empty list is the goal, not a requirement.
ANALYTICS_RAW_ALLOWED: dict[str, str] = {}


class FakeClock:
    """A clock and a sleep that agree with each other, so a burst test costs no seconds."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept += seconds
        self.now += seconds


def _limits() -> tuple[DeskLimits, FakeClock]:
    clock = FakeClock()
    return DeskLimits(clock=clock.time, sleep=clock.sleep), clock


class TestTheCapsAreKitesOwn:
    """Kite Connect publishes a cap per endpoint, not one for the whole API."""

    def test_the_families_and_their_rates(self) -> None:
        assert QUOTE_PER_SECOND == 1.0, "quote/ltp/ohlc is Kite's 1 req/s endpoint"
        assert HISTORICAL_PER_SECOND == 3.0, "historical_data is 3 req/s"
        assert GENERAL_PER_SECOND == 9.0, "everything else is 10/s; we keep one in hand"

    @pytest.mark.parametrize(
        ("call", "family"),
        [
            ("ltp", "quote"),
            ("quote", "quote"),
            ("ohlc", "quote"),
            ("historical_data", "historical"),
            ("holdings", "general"),
            ("positions", "general"),
            ("orders", "general"),
            ("trades", "general"),
            ("margins", "general"),
            ("instruments", "general"),
            ("profile", "general"),
            ("get_gtts", "general"),
        ],
    )
    def test_each_kite_method_is_filed_under_its_own_cap(self, call: str, family: str) -> None:
        assert FAMILY_FOR_CALL[call] == family

    def test_an_unknown_method_is_throttled_rather_than_free(self) -> None:
        """A method somebody adds tomorrow lands in the general pool, not outside every pool."""
        limits, clock = _limits()
        limits.slot_for_call("some_endpoint_kite_adds_in_2027")
        limits.slot_for_call("some_endpoint_kite_adds_in_2027")
        assert clock.slept == pytest.approx(1.0 / GENERAL_PER_SECOND, rel=1e-6)


class TestTheBurstFloor:
    def test_thirty_quotes_take_at_least_twenty_nine_seconds(self) -> None:
        limits, clock = _limits()
        for _ in range(30):
            limits.slot("quote")
        assert clock.slept == pytest.approx(29.0, rel=1e-6), "1 req/s, and the first is free"

    def test_thirty_historical_calls_take_at_least_nine_and_two_thirds(self) -> None:
        limits, clock = _limits()
        for _ in range(30):
            limits.slot("historical")
        assert clock.slept == pytest.approx(29 / 3, rel=1e-6)

    def test_thirty_general_calls_take_at_least_three_and_a_bit(self) -> None:
        limits, clock = _limits()
        for _ in range(30):
            limits.slot("general")
        assert clock.slept == pytest.approx(29 / 9, rel=1e-6)

    def test_one_slow_family_never_starves_another(self) -> None:
        """A backfill spending its 3/s must not make the monitor wait for a quote."""
        limits, clock = _limits()
        for _ in range(10):
            limits.slot("historical")
        before = clock.slept
        limits.slot("quote")
        assert clock.slept == before, "the first quote of the day waited on a historical pull"

    def test_the_first_call_of_each_family_does_not_wait(self) -> None:
        limits, clock = _limits()
        for family in ("quote", "historical", "general"):
            limits.slot(family)
        assert clock.slept == 0.0

    def test_two_threads_get_two_different_slots(self) -> None:
        """Real clock, real sleep, two threads: the second departure is a full interval later."""
        spacer = Spacer(20.0)
        waits: list[float] = []
        lock = threading.Lock()

        def call() -> None:
            waited = spacer.take()
            with lock:
                waits.append(waited)

        threads = [threading.Thread(target=call) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(waits) == 2
        assert max(waits) >= 0.04, f"both threads left at once: {waits}"

    def test_a_non_positive_rate_is_refused(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            Spacer(0)


class TestEveryReadInTheClientTakesASlot:
    """Over the source: every `self.kc.<attr>` access sits in a method that took a slot first.

    Written as a scan rather than as a call-count assertion because the failure it guards against
    is somebody adding a twenty-seventh method, and no behavioural test knows about a method that
    does not exist yet.
    """

    #: Attributes of `kc` that are constants or session plumbing, not requests: reading
    #: `kc.VARIETY_REGULAR` costs Kite nothing, and the auth calls are the login itself.
    NOT_A_REQUEST = frozenset(
        {
            "set_access_token",
            "generate_session",
            "login_url",
            "access_token",
            "VARIETY_REGULAR",
            "TRANSACTION_TYPE_BUY",
            "TRANSACTION_TYPE_SELL",
            "PRODUCT_CNC",
            "ORDER_TYPE_LIMIT",
            "ORDER_TYPE_MARKET",
            "VALIDITY_DAY",
            "GTT_TYPE_SINGLE",
        }
    )
    #: The order path's own calls: limited by `baskfy_execution.ratelimit.KiteLimits` inside the
    #: gateway, and by `kite_client`'s own guards here. Listing them keeps the scan honest about
    #: what it is NOT checking rather than pretending the file has no writes.
    ORDER_PATH = frozenset({"place_order", "place_gtt", "delete_gtt", "modify_gtt"})

    @staticmethod
    def _methods_with_kc_calls() -> dict[str, set[str]]:
        tree = ast.parse(inspect.getsource(KC))
        found: dict[str, set[str]] = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            names: set[str] = set()
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Attribute)
                    and isinstance(inner.value, ast.Attribute)
                    and inner.value.attr == "kc"
                    and isinstance(inner.value.value, ast.Name)
                    and inner.value.value.id == "self"
                ):
                    names.add(inner.attr)
            if names:
                found[node.name] = names
        return found

    @staticmethod
    def _takes_a_slot(method: str) -> bool:
        tree = ast.parse(inspect.getsource(KC))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == method:
                source = ast.unparse(node)
                return "self.limits.slot" in source or "self.limits.slot_for_call" in source
        return False

    def test_the_scan_sees_the_file_it_thinks_it_sees(self) -> None:
        found = self._methods_with_kc_calls()
        assert len(found) >= 10, f"the scan found only {sorted(found)}"

    def test_every_request_carrying_method_takes_a_slot(self) -> None:
        offenders: list[str] = []
        for method, calls in self._methods_with_kc_calls().items():
            requests = {c for c in calls if c not in self.NOT_A_REQUEST and c not in self.ORDER_PATH}
            if not requests:
                continue
            if not self._takes_a_slot(method):
                offenders.append(f"{method} calls {sorted(requests)} with no slot")
        assert offenders == [], "; ".join(offenders)

    def test_the_scan_fails_when_a_slot_is_removed(self) -> None:
        """The scan's own proof: a method with a bare `self.kc.holdings()` is caught."""
        tree = ast.parse("class K:\n    def read(self):\n        return self.kc.holdings()\n")
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
        assert "self.limits.slot" not in ast.unparse(node)


class TestAnalyticsGoesThroughTheClient:
    def test_no_analytics_module_calls_the_raw_client(self) -> None:
        offenders: list[str] = []
        for path in sorted((APP / "analytics").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Attribute)
                    and node.value.attr == "kc"
                    and node.attr not in ("VARIETY_REGULAR",)
                ):
                    reason = ANALYTICS_RAW_ALLOWED.get(path.name)
                    if reason is None:
                        offenders.append(f"{path.name}:{node.lineno} .kc.{node.attr}")
        assert offenders == [], (
            "these reach Kite without a slot; give them a wrapper on `Kite` or an entry in "
            f"ANALYTICS_RAW_ALLOWED with a reason: {offenders}"
        )

    def test_the_allow_list_names_nothing_that_has_gone_away(self) -> None:
        for name in ANALYTICS_RAW_ALLOWED:
            assert (APP / "analytics" / name).exists(), f"{name} is allow-listed but gone"

    @pytest.mark.parametrize(
        "wrapper", ["instruments", "historical", "margins", "orders", "order_history",
                    "get_gtts", "quote_raw", "ltp", "quotes", "holdings", "trades"]
    )
    def test_the_client_offers_the_wrapper_the_call_sites_need(self, wrapper: str) -> None:
        assert callable(getattr(KC.Kite, wrapper))


class TestTheWebsocketIsTheQuoteSource:
    """SW6/SW20: the monitor reads ticks, and the REST quote is a bounded fallback.

    Kite's guidance and its rate table point the same way — one websocket carries 3,000
    instruments and costs nothing per tick, while `quote` is the 1 req/s endpoint. A monitor that
    polled instead would spend its whole budget watching five names.
    """

    def test_a_subscription_longer_than_one_connection_is_refused(self) -> None:
        import asyncio

        from app.core.ticker import MAX_INSTRUMENTS_PER_CONNECTION, TickBus, start_ticker

        async def go() -> None:
            with pytest.raises(ValueError, match="one websocket"):
                start_ticker("k", "t", list(range(MAX_INSTRUMENTS_PER_CONNECTION + 1)), TickBus())

        asyncio.run(go())

    def test_the_monitors_fallback_asks_kite_at_most_once_per_window(self) -> None:
        """A hundred polls inside the window cost one call — `quote_poll_min_seconds` [5]."""
        import datetime as dt

        from app.swing_monitor import QuoteFallback

        calls: list[list[str]] = []

        class Source:
            def quote_raw(self, keys):
                calls.append(list(keys))
                return {k: {"last_price": 100.0, "ohlc": {}, "volume": 0} for k in keys}

        now = [1000.0]
        fallback = QuoteFallback(Source(), {1001: "ALPHAFLAG"}, clock=lambda: now[0])
        at = dt.datetime(2026, 9, 4, 9, 30)
        for _ in range(100):
            fallback.poll(at)
        assert fallback.calls == 1, f"{fallback.calls} Kite calls inside one window"
        assert len(calls) == 1

        now[0] += 5.01
        fallback.poll(at)
        assert fallback.calls == 2, "the window expired and no second call was made"

    def test_the_fallback_reaches_kite_through_the_limited_wrapper(self) -> None:
        """`quote_raw` takes the 1 req/s slot; `kc.quote` would not."""
        import inspect

        from app import swing_monitor

        source = inspect.getsource(swing_monitor.QuoteFallback)
        assert "quote_raw(" in source
        assert "kc.quote(" not in source
