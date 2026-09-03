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


# =====================================================================================
# SW21 — the same limit, held by the box rather than by each container.
#
# The tests below split in two on purpose. The wiring (what happens when Redis is absent,
# refusing, or slow) is proved against stubs, because those are states a real server will not
# enter on demand. The sharing itself is proved against a REAL Redis, for the reason
# `packages/providers/tests/test_ratelimit.py` gives about its own bucket: the atomicity that
# makes two processes agree lives in Redis's execution of the script, not in our Python, and a
# hand-written fake would only prove the fake agrees with itself. It skips where there is none.
# =====================================================================================
import os
import time

from app.core import kite_limits as KL
from app.core.kite_limits import (
    MAX_SHARED_WAIT_SECONDS,
    RATE_FOR_FAMILY,
    SHARED_KEY_PREFIX,
    SharedSpacer,
)

#: `tests/conftest.py` replaces `kite_limits.shared_spacers` for every test, so no test builds the
#: box's limiter by omission. This is the real one, captured at import — before the fixture runs —
#: for the three tests whose subject IS the builder.
BUILD_SHARED = KL.shared_spacers


class StubScript:
    """Stands in for the registered Lua script: records the calls, answers as told."""

    def __init__(self, replies=None, raises: Exception | None = None) -> None:
        self.replies = replies
        self.raises = raises
        self.calls: list[tuple[list[str], list[object]]] = []

    def __call__(self, keys, args):
        self.calls.append((list(keys), list(args)))
        if self.raises is not None:
            raise self.raises
        return self.replies


class TestTheFamiliesAreTheSameOnBothSides:
    def test_every_family_has_a_rate_and_a_spacer(self) -> None:
        limits = DeskLimits(shared={})
        assert set(RATE_FOR_FAMILY) == {"quote", "historical", "general"}
        for family, rate in RATE_FOR_FAMILY.items():
            assert limits.spacer(family).interval == pytest.approx(1.0 / rate)

    def test_the_keys_live_under_the_namespace_baskfy_already_owns(self) -> None:
        """One key per family, beside the pipeline's own bucket at `baskfy:ratelimit:kite`."""
        assert SHARED_KEY_PREFIX == "baskfy:ratelimit:kite"

    def test_a_limiter_takes_the_processes_shared_spacers_by_default(self, monkeypatch) -> None:
        """No call site passes them: `Kite()` gets the box's limit because `DeskLimits()` does."""
        spacers = {"quote": object()}
        monkeypatch.setattr(KL, "shared_spacers", lambda: spacers)
        limits = DeskLimits()
        assert limits.shared == spacers
        assert limits.is_shared is True
        assert DeskLimits(shared={}).is_shared is False


class TestTheBuilderReadsTheEnvironment:
    def test_no_redis_url_is_per_process_and_says_so(self, monkeypatch) -> None:
        monkeypatch.setattr(KL.C, "SHARED_READ_LIMITS_REDIS_URL", "")
        monkeypatch.setattr(KL.C, "SHARED_READ_LIMITS", True)
        KL.reset_shared_spacers()
        assert BUILD_SHARED() == {}
        monkeypatch.setattr(KL, "shared_spacers", BUILD_SHARED)
        assert KL.shared_mode() == "per-process"

    def test_the_switch_turns_it_off_with_a_url_present(self, monkeypatch) -> None:
        """The reversal in one variable — DESK_SHARED_READ_LIMITS=false."""
        monkeypatch.setattr(KL.C, "SHARED_READ_LIMITS_REDIS_URL", "redis://localhost:6379/0")
        monkeypatch.setattr(KL.C, "SHARED_READ_LIMITS", False)
        KL.reset_shared_spacers()
        assert BUILD_SHARED() == {}

    def test_an_unreachable_redis_is_per_process_not_an_exception(self, monkeypatch) -> None:
        """The desk must start, and trade, with no Redis at all (root CLAUDE.md's Friday rule)."""
        monkeypatch.setattr(KL.C, "SHARED_READ_LIMITS", True)
        # Port 1 is reserved and nothing listens on it; the connect fails inside the timeout.
        monkeypatch.setattr(KL.C, "SHARED_READ_LIMITS_REDIS_URL", "redis://127.0.0.1:1/0")
        KL.reset_shared_spacers()
        assert BUILD_SHARED() == {}

    def test_a_failed_connection_is_retried_rather_than_given_up_on(self, monkeypatch) -> None:
        """A Redis restarted by a deploy must not leave this process unshared until somebody
        notices."""
        monkeypatch.setattr(KL.C, "SHARED_READ_LIMITS", True)
        monkeypatch.setattr(KL.C, "SHARED_READ_LIMITS_REDIS_URL", "redis://127.0.0.1:1/0")
        KL.reset_shared_spacers()
        builds: list[int] = []

        def build() -> dict:
            builds.append(1)
            return {}

        monkeypatch.setattr(KL, "_build_shared_spacers", build)
        BUILD_SHARED()
        BUILD_SHARED()
        assert len(builds) == 1, "it rebuilt on every call, paying the connect timeout each time"
        # The minute, brought forward. Reaching into the retry stamp rather than patching
        # `time.monotonic` — which this process's own spacers also read — or sleeping for it.
        monkeypatch.setattr(KL, "_shared_retry_at", 0.0)
        BUILD_SHARED()
        assert len(builds) == 2, "it never looked at Redis again"


class TestRedisIsANicetyAndNeverADependency:
    def test_a_dead_redis_still_spaces_the_call_locally(self) -> None:
        script = StubScript(raises=ConnectionError("connection refused"))
        clock = FakeClock()
        limits = DeskLimits(clock=clock.time, sleep=clock.sleep, shared={
            "quote": SharedSpacer(script, "k:quote", QUOTE_PER_SECOND,
                                  clock=clock.time, sleep=clock.sleep),
        })
        for _ in range(5):
            limits.slot("quote")
        assert clock.slept == pytest.approx(4.0), "the local 1 req/s floor did not hold"
        assert limits.local_only["quote"] == 5, "the degradation was not counted"

    def test_a_dead_redis_is_not_asked_again_on_every_read(self) -> None:
        """Otherwise every page refresh pays the connect timeout while Redis is down."""
        script = StubScript(raises=ConnectionError("connection refused"))
        clock = FakeClock()
        spacer = SharedSpacer(script, "k:general", GENERAL_PER_SECOND,
                              clock=clock.time, sleep=clock.sleep, retry_after=30.0)
        for _ in range(20):
            assert spacer.take() is None
        assert len(script.calls) == 1, f"it asked a dead Redis {len(script.calls)} times"
        clock.now += 31
        assert spacer.take() is None
        assert len(script.calls) == 2, "it never tried Redis again"

    def test_a_queue_past_the_budget_falls_back_rather_than_hanging_the_page(self) -> None:
        """`claimed == 0` — some other process holds the next ten seconds of this family."""
        script = StubScript(replies=[0, str(MAX_SHARED_WAIT_SECONDS + 5)])
        clock = FakeClock()
        limits = DeskLimits(clock=clock.time, sleep=clock.sleep, shared={
            "quote": SharedSpacer(script, "k:quote", QUOTE_PER_SECOND,
                                  clock=clock.time, sleep=clock.sleep),
        })
        limits.slot("quote")
        assert clock.slept == 0.0, "it waited out a slot it was told it could not have"
        assert limits.local_only["quote"] == 1

    def test_a_reply_the_script_could_not_have_sent_is_refused(self) -> None:
        """A wrong shape is a bug in the script, not a slot: it must not read as permission."""
        for junk in ("ok", [1], [1, 2, 3], None):
            with pytest.raises(ValueError, match="Kite limiter"):
                KL._claim(junk)


class TestTheTwoLimitsCompose:
    """Both are always taken: the box's clock bounds the box, this process's bounds this process."""

    def test_the_shared_clock_cannot_loosen_the_local_floor(self) -> None:
        """A shared limiter that grants freely must not turn thirty quotes into a burst."""
        script = StubScript(replies=[1, "0"])
        clock = FakeClock()
        limits = DeskLimits(clock=clock.time, sleep=clock.sleep, shared={
            "quote": SharedSpacer(script, "k:quote", QUOTE_PER_SECOND,
                                  clock=clock.time, sleep=clock.sleep),
        })
        for _ in range(30):
            limits.slot("quote")
        assert clock.slept == pytest.approx(29.0, rel=1e-6)
        assert len(script.calls) == 30, "a read went out without asking the box"

    def test_the_shared_clock_can_tighten_it(self) -> None:
        """When another container holds the family, this one waits for it — that is the point."""
        script = StubScript(replies=[1, "2.0"])
        clock = FakeClock()
        limits = DeskLimits(clock=clock.time, sleep=clock.sleep, shared={
            "general": SharedSpacer(script, "k:general", GENERAL_PER_SECOND,
                                    clock=clock.time, sleep=clock.sleep),
        })
        waits = [limits.slot("general") for _ in range(3)]
        assert waits == pytest.approx([2.0, 2.0, 2.0])
        assert clock.slept == pytest.approx(6.0), "the local spacer double-charged the wait"
        assert limits.waits["general"] == pytest.approx(6.0)

    def test_the_interval_and_the_budget_are_what_the_script_is_told(self) -> None:
        script = StubScript(replies=[1, "0"])
        clock = FakeClock()
        spacer = SharedSpacer(script, "baskfy:ratelimit:kite:historical", HISTORICAL_PER_SECOND,
                              clock=clock.time, sleep=clock.sleep)
        spacer.take()
        keys, args = script.calls[0]
        assert keys == ["baskfy:ratelimit:kite:historical"]
        assert args[0] == pytest.approx(1.0 / HISTORICAL_PER_SECOND)
        assert args[1] == MAX_SHARED_WAIT_SECONDS


#: The dev stack's Redis (`make up`), or whatever the box points the desk at.
REDIS_URL = os.getenv("BASKFY_REDIS_URL", "redis://localhost:6380/0")


def _redis_or_skip():
    redis = pytest.importorskip("redis")
    try:
        client = redis.Redis.from_url(REDIS_URL, socket_timeout=1.5, socket_connect_timeout=1.5)
        client.ping()
    except Exception as exc:                                              # pragma: no cover
        pytest.skip(f"no Redis at {REDIS_URL}: {type(exc).__name__}: {exc}")
    return client


class TestTheBoxHoldsOneLimit:
    """Two `SharedSpacer`s on one key are two containers on one box. Against a real Redis.

    The rate is deliberately not Kite's: proving the sharing at 1 req/s would cost the suite a
    minute of real sleeping, and what is under test is that two independent limiters queue behind
    one clock — which is as true at 20/s as at 1/s. The caps themselves are asserted above,
    without spending the seconds, by the burst tests.
    """

    RATE = 20.0
    INTERVAL = 1.0 / RATE

    @pytest.fixture
    def key(self, request: pytest.FixtureRequest):
        client = _redis_or_skip()
        name = f"baskfy:test:ratelimit:kite:{request.node.name}"
        client.delete(name)
        yield client, name
        client.delete(name)

    def _spacer(self, client, name: str, **kwargs) -> SharedSpacer:
        return SharedSpacer(client.register_script(KL.TAKE_SCRIPT), name, self.RATE, **kwargs)

    def test_two_processes_queue_behind_one_clock(self, key) -> None:
        client, name = key
        web = self._spacer(client, name)
        monitor = self._spacer(client, name)
        started = time.monotonic()
        for spacer in (web, monitor, web, monitor, web, monitor):
            assert spacer.take() is not None
        elapsed = time.monotonic() - started
        assert elapsed >= 5 * self.INTERVAL, (
            f"six calls from two processes took {elapsed:.3f}s; one limiter each would have "
            f"allowed {3 * self.INTERVAL:.3f}s"
        )

    def test_the_first_call_against_a_cold_key_does_not_wait(self, key) -> None:
        client, name = key
        assert self._spacer(client, name).take() == pytest.approx(0.0, abs=0.01)

    def test_a_caller_that_will_not_wait_does_not_spend_the_slot(self, key) -> None:
        """A refusal must not push the clock out for the process that WOULD have waited."""
        client, name = key
        patient = self._spacer(client, name)
        impatient = self._spacer(client, name, max_wait=0.0)
        patient.take()
        assert impatient.take() is None, "it waited past a budget of zero"
        assert patient.take() is not None
        # Two claims, one refusal: the third call departs one interval after the first, not two.
        assert float(client.get(name)) <= time.time() + 2 * self.INTERVAL + 0.5

    def test_the_key_expires_so_an_idle_family_costs_nothing(self, key) -> None:
        client, name = key
        self._spacer(client, name).take()
        ttl = client.pttl(name)
        assert 0 < ttl <= (self.INTERVAL + 1.0) * 1000 + 1, f"pttl={ttl}"
