"""KiteProvider (Prompt 2 deliverable 2, acceptance criterion 3).

    "A test proves KiteProvider retries and eventually raises a typed error after N attempts."

Everything here runs against a fake client injected through :class:`KiteRuntime`, so no network
call is possible (acceptance criterion 1) and every failure mode — including ones a live broker
would only produce at 3am — is reachable.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from itertools import pairwise

import polars as pl
import pytest
from kiteconnect import exceptions as kite_exceptions

from baskfy_providers.errors import (
    AccessTokenExpired,
    CredentialsMissing,
    ProviderUnavailable,
    RateLimited,
    RetryBudgetExhausted,
    UnexpectedPayload,
    UpstreamUnavailable,
)
from baskfy_providers.kite import DEFAULT_MAX_DAYS_PER_REQUEST, KiteProvider, KiteRuntime
from baskfy_providers.ports import BARS_CAPABILITIES, HOLDINGS_CAPABILITIES, Capability
from baskfy_providers.records import DAILY_BARS_SCHEMA, BrokerAccountRef, InstrumentRecord
from baskfy_providers.retry import RetryHooks, RetryPolicy
from baskfy_providers.settings import ProviderSettings
from baskfy_providers.tokens import IST, AccessTokenStore

#: This morning, in IST — not a pinned date.
#:
#: `AccessToken.is_expired` compares calendar dates in IST, because "Kite invalidates access
#: tokens at the start of the next trading day" (`baskfy_providers.tokens`). A literal date here
#: therefore stops meaning "fresh" the moment the clock passes midnight IST, and every test that
#: relies on a fresh token starts failing for a reason that has nothing to do with the code. The
#: relative offsets below (`- 1 day`, `- 2 days`) are what these tests are actually about.
TODAY = dt.datetime.now(tz=IST).replace(hour=9, minute=0, second=0, microsecond=0)


class FakeKiteClient:
    """Records calls, and fails in whatever way the test asks for."""

    def __init__(  # noqa: PLR0913 - a test double is configured, not called
        self,
        *,
        candles: list[dict[str, object]] | None = None,
        instruments: list[dict[str, object]] | None = None,
        raises: Exception | None = None,
        fail_times: int | None = None,
        positions: list[dict[str, object]] | None = None,
        margins: dict[str, object] | None = None,
    ) -> None:
        self._candles = candles or []
        self._instruments = instruments or []
        self._raises = raises
        self._fail_times = fail_times
        self._positions = positions or []
        self._margins = margins or {}
        self.access_tokens: list[str] = []
        self.historical_calls: list[tuple[int, dt.date, dt.date, str]] = []
        self.instrument_calls: int = 0
        self.holdings_calls: int = 0
        self.margin_segments: list[str | None] = []

    def set_access_token(self, access_token: str) -> None:
        self.access_tokens.append(access_token)

    def _maybe_raise(self) -> None:
        if self._raises is None:
            return
        if self._fail_times is None:
            raise self._raises
        if len(self.historical_calls) + self.instrument_calls <= self._fail_times:
            raise self._raises

    def instruments(self, exchange: str | None = None) -> list[dict[str, object]]:
        self.instrument_calls += 1
        self._maybe_raise()
        del exchange
        return self._instruments

    def historical_data(
        self,
        instrument_token: int,
        from_date: dt.date | dt.datetime | str,
        to_date: dt.date | dt.datetime | str,
        interval: str,
    ) -> list[dict[str, object]]:
        assert isinstance(from_date, dt.date)
        assert isinstance(to_date, dt.date)
        self.historical_calls.append((instrument_token, from_date, to_date, interval))
        self._maybe_raise()
        return [candle for candle in self._candles if from_date <= _candle_date(candle) <= to_date]

    def holdings(self) -> list[dict[str, object]]:
        self.holdings_calls += 1
        self._maybe_raise()
        return self._positions

    def margins(self, segment: str | None = None) -> dict[str, object]:
        self.margin_segments.append(segment)
        self._maybe_raise()
        return self._margins


def _candle_date(candle: dict[str, object]) -> dt.date:
    value = candle["date"]
    assert isinstance(value, dt.date)
    return value


class UnlimitedBucket:
    """A limiter that never blocks — the throttling itself is tested in test_ratelimit.py.

    Satisfies the ``RateLimiter`` Protocol structurally, which is the point of that Protocol:
    an adapter's throttling seam should be replaceable without inheriting a Redis client.
    """

    def __init__(self) -> None:
        self.acquisitions = 0

    def acquire(self, tokens: float = 1.0) -> float:
        del tokens
        self.acquisitions += 1
        return 0.0


@pytest.fixture
def stored_token(configured_settings: ProviderSettings) -> AccessTokenStore:
    store = AccessTokenStore(
        configured_settings.kite_token_path, configured_settings.kite_token_encryption_key
    )
    store.save("fresh-token", issued_at=TODAY)
    return store


def build_provider(
    settings: ProviderSettings,
    client: FakeKiteClient,
    store: AccessTokenStore,
    *,
    policy: RetryPolicy | None = None,
    sleeps: list[float] | None = None,
) -> KiteProvider:
    hooks = RetryHooks(
        sleeper=(sleeps.append if sleeps is not None else lambda _: None),
        jitter=lambda ceiling: ceiling,
    )
    return KiteProvider(
        settings,
        KiteRuntime(
            rate_limiter=UnlimitedBucket(),
            client_factory=lambda _api_key: client,
            token_store=store,
            retry_policy=policy or RetryPolicy(max_attempts=3, base_seconds=0.5),
            retry_hooks=hooks,
        ),
    )


class TestCapabilities:
    def test_it_offers_bars_and_the_broker_ledger(self, settings: ProviderSettings) -> None:
        """docs/02: Kite has no constituents, no PE/PB, no corporate actions. It must not claim
        them, or CompositeProvider would route reference calls into a dead end.

        It does serve the read-only broker ledger (PORTFOLIO_REDESIGN.md §4.6 layer 1), which is
        the one thing Kite knows that NSE cannot: what this user actually holds.
        """
        provider = KiteProvider(settings)
        assert provider.capabilities() == BARS_CAPABILITIES | HOLDINGS_CAPABILITIES
        assert Capability.INDEX_CONSTITUENTS not in provider.capabilities()
        assert Capability.CORPORATE_ACTIONS not in provider.capabilities()


class TestHealth:
    def test_unconfigured_is_unavailable_not_an_exception(self, settings: ProviderSettings) -> None:
        health = KiteProvider(settings).check()
        assert health.available is False
        assert "BASKFY_KITE_API_KEY" in health.detail

    def test_missing_token_is_reported(self, configured_settings: ProviderSettings) -> None:
        health = KiteProvider(configured_settings).check()
        assert health.available is False
        assert "no encrypted access token" in health.detail

    def test_expired_token_is_reported(self, configured_settings: ProviderSettings) -> None:
        """docs/09 calls token expiry "the #1 pipeline failure"; the doctor must surface it."""
        store = AccessTokenStore(
            configured_settings.kite_token_path, configured_settings.kite_token_encryption_key
        )
        store.save("stale", issued_at=TODAY - dt.timedelta(days=2))
        health = KiteProvider(
            configured_settings, KiteRuntime(rate_limiter=UnlimitedBucket(), token_store=store)
        ).check()
        assert health.available is False
        assert "expired" in health.detail.lower()

    def test_without_a_limiter_it_refuses_to_be_available(
        self, configured_settings: ProviderSettings, stored_token: AccessTokenStore
    ) -> None:
        """docs/09 caps Kite at ~3 req/s; calling it unthrottled risks the API key."""
        health = KiteProvider(configured_settings, KiteRuntime(token_store=stored_token)).check()
        assert health.available is False
        assert "rate limiter" in health.detail

    def test_fully_configured_is_available(
        self, configured_settings: ProviderSettings, stored_token: AccessTokenStore
    ) -> None:
        provider = KiteProvider(
            configured_settings,
            KiteRuntime(rate_limiter=UnlimitedBucket(), token_store=stored_token),
        )
        assert provider.check().available is True


class TestTokenHandling:
    def test_an_expired_token_stops_the_call_before_it_is_made(
        self, configured_settings: ProviderSettings
    ) -> None:
        """A known-dead token must never reach the wire; it would burn the rate limit."""
        store = AccessTokenStore(
            configured_settings.kite_token_path, configured_settings.kite_token_encryption_key
        )
        store.save("stale", issued_at=TODAY - dt.timedelta(days=1))
        client = FakeKiteClient()
        provider = build_provider(configured_settings, client, store)
        with pytest.raises(AccessTokenExpired):
            provider.list_instruments()
        assert client.instrument_calls == 0

    def test_a_missing_api_key_raises_credentials_missing(self, settings: ProviderSettings) -> None:
        store = AccessTokenStore(settings.kite_token_path, "")
        provider = build_provider(settings, FakeKiteClient(), store)
        with pytest.raises(CredentialsMissing):
            provider.list_instruments()

    def test_token_expiry_is_never_retried(self, configured_settings: ProviderSettings) -> None:
        """docs/09 wants a human paged. Five retries only delay the alert."""
        store = AccessTokenStore(
            configured_settings.kite_token_path, configured_settings.kite_token_encryption_key
        )
        store.save("fresh", issued_at=TODAY)
        client = FakeKiteClient(raises=kite_exceptions.TokenException("token expired"))
        sleeps: list[float] = []
        provider = build_provider(configured_settings, client, store, sleeps=sleeps)
        with pytest.raises(AccessTokenExpired):
            provider.list_instruments()
        assert client.instrument_calls == 1
        assert sleeps == []


class TestRetries:
    """Acceptance criterion 3, stated directly."""

    def test_it_retries_then_raises_a_typed_error_after_n_attempts(
        self, configured_settings: ProviderSettings, stored_token: AccessTokenStore
    ) -> None:
        attempts = 4
        client = FakeKiteClient(raises=kite_exceptions.NetworkException("503 from upstream"))
        sleeps: list[float] = []
        provider = build_provider(
            configured_settings,
            client,
            stored_token,
            policy=RetryPolicy(max_attempts=attempts, base_seconds=0.5),
            sleeps=sleeps,
        )

        with pytest.raises(RetryBudgetExhausted) as raised:
            provider.list_instruments()

        assert client.instrument_calls == attempts
        assert len(sleeps) == attempts - 1
        assert raised.value.attempts == attempts
        assert isinstance(raised.value.last_error, UpstreamUnavailable)
        assert "503 from upstream" in str(raised.value)

    def test_backoff_is_exponential(
        self, configured_settings: ProviderSettings, stored_token: AccessTokenStore
    ) -> None:
        client = FakeKiteClient(raises=kite_exceptions.NetworkException("503"))
        sleeps: list[float] = []
        provider = build_provider(
            configured_settings,
            client,
            stored_token,
            policy=RetryPolicy(max_attempts=5, base_seconds=0.5, max_seconds=30),
            sleeps=sleeps,
        )
        with pytest.raises(RetryBudgetExhausted):
            provider.list_instruments()
        assert sleeps == [0.5, 1.0, 2.0, 4.0]

    def test_a_transient_failure_that_clears_is_not_surfaced(
        self, configured_settings: ProviderSettings, stored_token: AccessTokenStore
    ) -> None:
        """The point of retrying: two flaky calls must not fail a nightly run."""
        client = FakeKiteClient(
            instruments=[
                {
                    "tradingsymbol": "CUPID",
                    "name": "CUPID LIMITED",
                    "instrument_type": "EQ",
                    "segment": "NSE",
                    "instrument_token": 12345,
                    "exchange": "NSE",
                }
            ],
            raises=kite_exceptions.NetworkException("flaky"),
            fail_times=2,
        )
        provider = build_provider(
            configured_settings,
            client,
            stored_token,
            policy=RetryPolicy(max_attempts=5, base_seconds=0.1),
        )
        records = provider.list_instruments()
        assert [r.symbol for r in records] == ["CUPID"]
        assert client.instrument_calls == 3

    def test_a_malformed_request_is_not_retried(
        self, configured_settings: ProviderSettings, stored_token: AccessTokenStore
    ) -> None:
        """Our bug, not theirs. Retrying it wastes the rate limit and hides the cause."""
        client = FakeKiteClient(raises=kite_exceptions.InputException("bad interval"))
        provider = build_provider(configured_settings, client, stored_token)
        with pytest.raises(UnexpectedPayload):
            provider.list_instruments()
        assert client.instrument_calls == 1

    def test_throttling_is_translated_to_rate_limited(
        self, configured_settings: ProviderSettings, stored_token: AccessTokenStore
    ) -> None:
        """Kite signals throttling inside NetworkException; the type must still be specific."""
        client = FakeKiteClient(
            raises=kite_exceptions.NetworkException("Too many requests"), fail_times=1
        )
        provider = build_provider(
            configured_settings,
            client,
            stored_token,
            policy=RetryPolicy(max_attempts=1, base_seconds=0.1),
        )
        with pytest.raises(RetryBudgetExhausted) as raised:
            provider.list_instruments()
        assert isinstance(raised.value.last_error, RateLimited)

    def test_a_permission_failure_is_unavailable_not_transient(
        self, configured_settings: ProviderSettings, stored_token: AccessTokenStore
    ) -> None:
        client = FakeKiteClient(raises=kite_exceptions.PermissionException("no historical access"))
        provider = build_provider(configured_settings, client, stored_token)
        with pytest.raises(ProviderUnavailable):
            provider.list_instruments()
        assert client.instrument_calls == 1


class TestChunking:
    """docs/09: "chunk backfills into <= 2000-day slices per instrument"."""

    def test_the_default_cap_matches_the_doc(self) -> None:
        assert DEFAULT_MAX_DAYS_PER_REQUEST == 2000

    def test_a_short_window_is_one_request(
        self, configured_settings: ProviderSettings, stored_token: AccessTokenStore
    ) -> None:
        provider = build_provider(configured_settings, FakeKiteClient(), stored_token)
        windows = list(provider.chunk_windows(dt.date(2026, 1, 1), dt.date(2026, 3, 1)))
        assert windows == [(dt.date(2026, 1, 1), dt.date(2026, 3, 1))]

    def test_a_long_backfill_is_split_at_the_cap(
        self, tmp_path_factory: pytest.TempPathFactory, stored_token: AccessTokenStore
    ) -> None:
        settings = ProviderSettings(_env_file=None, kite_max_days_per_request=100)
        provider = build_provider(settings, FakeKiteClient(), stored_token)
        windows = list(provider.chunk_windows(dt.date(2020, 1, 1), dt.date(2020, 12, 31)))
        assert len(windows) == 4
        for start, end in windows:
            assert (end - start).days < 100
        del tmp_path_factory

    def test_chunks_are_contiguous_and_cover_the_whole_window(
        self, configured_settings: ProviderSettings, stored_token: AccessTokenStore
    ) -> None:
        """A gap here silently loses bars; an overlap silently double-counts volume."""
        settings = configured_settings.model_copy(update={"kite_max_days_per_request": 90})
        provider = build_provider(settings, FakeKiteClient(), stored_token)
        start, end = dt.date(2011, 1, 1), dt.date(2013, 6, 30)
        windows = list(provider.chunk_windows(start, end))
        assert windows[0][0] == start
        assert windows[-1][1] == end
        for previous, following in pairwise(windows):
            assert following[0] == previous[1] + dt.timedelta(days=1)

    def test_daily_bars_issues_one_request_per_chunk(
        self, configured_settings: ProviderSettings, stored_token: AccessTokenStore
    ) -> None:
        settings = configured_settings.model_copy(update={"kite_max_days_per_request": 100})
        client = FakeKiteClient(candles=_candles(dt.date(2020, 1, 1), 300))
        provider = build_provider(settings, client, stored_token)
        frame = provider.daily_bars(555, dt.date(2020, 1, 1), dt.date(2020, 10, 26))
        assert len(client.historical_calls) == 3
        assert frame.height == 300

    def test_an_inverted_window_is_rejected(
        self, configured_settings: ProviderSettings, stored_token: AccessTokenStore
    ) -> None:
        provider = build_provider(configured_settings, FakeKiteClient(), stored_token)
        with pytest.raises(ValueError, match="precedes"):
            provider.daily_bars(1, dt.date(2026, 2, 1), dt.date(2026, 1, 1))


class TestBarFrame:
    def test_the_schema_is_fixed(
        self, configured_settings: ProviderSettings, stored_token: AccessTokenStore
    ) -> None:
        """Prompt 2 §2: "returns a Polars DataFrame with a fixed schema"."""
        client = FakeKiteClient(candles=_candles(dt.date(2026, 8, 3), 5))
        provider = build_provider(configured_settings, client, stored_token)
        frame = provider.daily_bars(42, dt.date(2026, 8, 1), dt.date(2026, 8, 18))
        assert dict(frame.schema) == DAILY_BARS_SCHEMA

    def test_prices_are_decimals_not_floats(
        self, configured_settings: ProviderSettings, stored_token: AccessTokenStore
    ) -> None:
        """docs/04: money in numeric, never float — starting at the provider boundary."""
        client = FakeKiteClient(candles=_candles(dt.date(2026, 8, 3), 2))
        provider = build_provider(configured_settings, client, stored_token)
        frame = provider.daily_bars(42, dt.date(2026, 8, 1), dt.date(2026, 8, 18))
        assert isinstance(frame["close"].dtype, pl.Decimal)
        assert frame["close"][0] == Decimal("101.0000")

    def test_the_source_column_marks_the_bars_as_raw_kite_data(
        self, configured_settings: ProviderSettings, stored_token: AccessTokenStore
    ) -> None:
        """docs/09: "Treat everything from Kite as raw." The provenance travels with the row."""
        client = FakeKiteClient(candles=_candles(dt.date(2026, 8, 3), 1))
        provider = build_provider(configured_settings, client, stored_token)
        frame = provider.daily_bars(42, dt.date(2026, 8, 1), dt.date(2026, 8, 18))
        assert frame["source"].unique().to_list() == ["kite"]

    def test_no_history_yields_an_empty_frame_with_the_schema(
        self, configured_settings: ProviderSettings, stored_token: AccessTokenStore
    ) -> None:
        """A young listing has no bars; docs/05 makes that NULL factors, not a failure."""
        provider = build_provider(configured_settings, FakeKiteClient(candles=[]), stored_token)
        frame = provider.daily_bars(42, dt.date(2026, 8, 1), dt.date(2026, 8, 18))
        assert frame.height == 0
        assert dict(frame.schema) == DAILY_BARS_SCHEMA

    def test_overlapping_chunks_do_not_duplicate_bars(
        self, configured_settings: ProviderSettings, stored_token: AccessTokenStore
    ) -> None:
        settings = configured_settings.model_copy(update={"kite_max_days_per_request": 10})
        client = FakeKiteClient(candles=_candles(dt.date(2026, 1, 1), 40))
        provider = build_provider(settings, client, stored_token)
        frame = provider.daily_bars(42, dt.date(2026, 1, 1), dt.date(2026, 2, 9))
        assert frame["date"].n_unique() == frame.height


class TestInstrumentMapping:
    def test_equities_and_indices_are_kept_and_derivatives_dropped(
        self, configured_settings: ProviderSettings, stored_token: AccessTokenStore
    ) -> None:
        """docs/04 constrains instrument_type to EQ | ETF | INDEX; futures are out of scope."""
        client = FakeKiteClient(
            instruments=[
                {
                    "tradingsymbol": "SBIN",
                    "name": "STATE BANK",
                    "instrument_type": "EQ",
                    "segment": "NSE",
                    "instrument_token": 1,
                },
                {
                    "tradingsymbol": "NIFTY 50",
                    "name": "NIFTY 50",
                    "instrument_type": "EQ",
                    "segment": "INDICES",
                    "instrument_token": 2,
                },
                {
                    "tradingsymbol": "SBIN26AUGFUT",
                    "name": "SBIN",
                    "instrument_type": "FUT",
                    "segment": "NFO-FUT",
                    "instrument_token": 3,
                },
            ]
        )
        provider = build_provider(configured_settings, client, stored_token)
        records = provider.list_instruments()
        assert [(r.symbol, r.instrument_type) for r in records] == [
            ("SBIN", "EQ"),
            ("NIFTY 50", "INDEX"),
        ]

    def test_a_row_with_no_symbol_is_dropped(
        self, configured_settings: ProviderSettings, stored_token: AccessTokenStore
    ) -> None:
        client = FakeKiteClient(
            instruments=[{"tradingsymbol": "", "instrument_type": "EQ", "segment": "NSE"}]
        )
        provider = build_provider(configured_settings, client, stored_token)
        assert provider.list_instruments() == []


class TestThrottling:
    def test_every_call_takes_a_token(
        self, configured_settings: ProviderSettings, stored_token: AccessTokenStore
    ) -> None:
        limiter = UnlimitedBucket()
        provider = KiteProvider(
            configured_settings,
            KiteRuntime(
                rate_limiter=limiter,
                client_factory=lambda _k: FakeKiteClient(candles=_candles(dt.date(2026, 8, 3), 1)),
                token_store=stored_token,
            ),
        )
        provider.daily_bars(42, dt.date(2026, 8, 1), dt.date(2026, 8, 18))
        assert limiter.acquisitions == 1

    def test_without_a_limiter_calls_are_refused(
        self, configured_settings: ProviderSettings, stored_token: AccessTokenStore
    ) -> None:
        provider = KiteProvider(
            configured_settings,
            KiteRuntime(client_factory=lambda _k: FakeKiteClient(), token_store=stored_token),
        )
        with pytest.raises(ProviderUnavailable, match="rate limiter"):
            provider.list_instruments()


def _candles(start: dt.date, count: int) -> list[dict[str, object]]:
    return [
        {
            "date": start + dt.timedelta(days=offset),
            "open": 100 + offset,
            "high": 105 + offset,
            "low": 95 + offset,
            "close": 101 + offset,
            "volume": 1000 + offset,
        }
        for offset in range(count)
    ]


class TestBrokerHoldings:
    """PORTFOLIO_REDESIGN.md §4.6 layer 1, read through the fake client — never the network."""

    def test_it_sums_settled_t1_and_collateral(
        self,
        configured_settings: ProviderSettings,
        stored_token: AccessTokenStore,
    ) -> None:
        """Desk non-negotiable #2: total = quantity + t1_quantity + collateral_quantity.

        Reading ``quantity`` alone would see a sell of the pledged 30 that never happened, and
        the sync above this would ask the user about it — which is the failure mode this rule
        exists to prevent.
        """
        client = FakeKiteClient(
            positions=[
                {
                    "tradingsymbol": "SBIN",
                    "exchange": "NSE",
                    "isin": "INE062A01020",
                    "quantity": 60,
                    "t1_quantity": 10,
                    "collateral_quantity": 30,
                    "average_price": 512.25,
                    "last_price": 540.10,
                    "product": "CNC",
                }
            ]
        )
        provider = build_provider(configured_settings, client, stored_token)

        rows = provider.broker_holdings(_ref())

        assert [row.symbol for row in rows] == ["SBIN"]
        assert rows[0].total_quantity == Decimal("100")
        assert rows[0].quantity == Decimal("60")
        assert rows[0].collateral_quantity == Decimal("30")

    def test_prices_arrive_as_decimal_not_float(
        self,
        configured_settings: ProviderSettings,
        stored_token: AccessTokenStore,
    ) -> None:
        """House rule 9: money is never a float, and the boundary is where that is decided."""
        client = FakeKiteClient(
            positions=[{"tradingsymbol": "INFY", "quantity": 5, "average_price": 1477.35}]
        )
        provider = build_provider(configured_settings, client, stored_token)

        row = provider.broker_holdings(_ref())[0]

        assert isinstance(row.average_price, Decimal)
        assert row.average_price == Decimal("1477.35")

    def test_a_missing_average_price_is_unknown_not_zero(
        self,
        configured_settings: ProviderSettings,
        stored_token: AccessTokenStore,
    ) -> None:
        """§5.2 forbids since-purchase P&L for a holding whose buy price we do not know.

        A zero would silently unlock exactly that number, and it would be wrong.
        """
        client = FakeKiteClient(positions=[{"tradingsymbol": "INFY", "quantity": 5}])
        provider = build_provider(configured_settings, client, stored_token)

        assert provider.broker_holdings(_ref())[0].average_price is None

    def test_one_unreadable_row_does_not_lose_the_others(
        self,
        configured_settings: ProviderSettings,
        stored_token: AccessTokenStore,
    ) -> None:
        """A new instrument class must not cost the user the sync of everything else they own."""
        client = FakeKiteClient(
            positions=[
                {"tradingsymbol": "", "quantity": 1},
                {"quantity": 2},
                {"tradingsymbol": "TCS", "quantity": 3},
            ]
        )
        provider = build_provider(configured_settings, client, stored_token)

        assert [row.symbol for row in provider.broker_holdings(_ref())] == ["TCS"]

    def test_an_empty_account_is_an_answer_not_an_error(
        self,
        configured_settings: ProviderSettings,
        stored_token: AccessTokenStore,
    ) -> None:
        """ "They sold everything" and "the fetch failed" must never be the same value."""
        provider = build_provider(configured_settings, FakeKiteClient(positions=[]), stored_token)
        assert provider.broker_holdings(_ref()) == []

    def test_a_token_failure_is_raised_not_swallowed(
        self,
        configured_settings: ProviderSettings,
        stored_token: AccessTokenStore,
    ) -> None:
        """docs/09 calls token expiry the #1 pipeline failure; it must be loud here too."""
        client = FakeKiteClient(raises=kite_exceptions.TokenException("token expired"))
        provider = build_provider(configured_settings, client, stored_token)

        with pytest.raises(AccessTokenExpired):
            provider.broker_holdings(_ref())

    def test_another_brokers_account_is_refused(
        self,
        configured_settings: ProviderSettings,
        stored_token: AccessTokenStore,
    ) -> None:
        """A Kite session cannot fetch Upstox holdings, and must not pretend it just did."""
        client = FakeKiteClient(positions=[{"tradingsymbol": "TCS", "quantity": 1}])
        provider = build_provider(configured_settings, client, stored_token)

        with pytest.raises(ProviderUnavailable, match="upstox"):
            provider.broker_holdings(_ref(broker_id="upstox"))
        assert client.holdings_calls == 0

    def test_another_tenants_account_is_refused_when_the_owner_is_known(
        self,
        configured_settings: ProviderSettings,
        stored_token: AccessTokenStore,
    ) -> None:
        """The read-side of "the gateway refuses a mismatch" (the two laws, multi-tenant clause).

        P4.2 is not built, so this adapter holds one token. Where the deployment knows whose it
        is, a read for anyone else is refused rather than answered with the wrong money.
        """
        client = FakeKiteClient(positions=[{"tradingsymbol": "TCS", "quantity": 1}])
        provider = KiteProvider(
            configured_settings,
            KiteRuntime(
                rate_limiter=UnlimitedBucket(),
                client_factory=lambda _api_key: client,
                token_store=stored_token,
                holdings_account_id=7,
            ),
        )

        assert provider.broker_holdings(_ref(broker_account_id=7))[0].symbol == "TCS"
        with pytest.raises(ProviderUnavailable, match="another tenant"):
            provider.broker_holdings(_ref(broker_account_id=8))


class TestBrokerCash:
    def test_it_reads_the_equity_segments_net_balance(
        self,
        configured_settings: ProviderSettings,
        stored_token: AccessTokenStore,
    ) -> None:
        client = FakeKiteClient(margins={"net": 12345.67, "available": {"cash": 12345.67}})
        provider = build_provider(configured_settings, client, stored_token)

        assert provider.broker_cash(_ref()) == Decimal("12345.67")
        assert client.margin_segments == ["equity"]

    def test_it_reads_the_whole_envelope_too(
        self,
        configured_settings: ProviderSettings,
        stored_token: AccessTokenStore,
    ) -> None:
        """One level of nesting must not cost the user their cash balance."""
        client = FakeKiteClient(margins={"equity": {"net": 900.5}, "commodity": {"net": 1.0}})
        provider = build_provider(configured_settings, client, stored_token)

        assert provider.broker_cash(_ref()) == Decimal("900.5")

    def test_no_reported_balance_is_none_not_zero(
        self,
        configured_settings: ProviderSettings,
        stored_token: AccessTokenStore,
    ) -> None:
        """§4.4's bucket is part of net worth. A zero we inferred would take real money away."""
        provider = build_provider(configured_settings, FakeKiteClient(margins={}), stored_token)
        assert provider.broker_cash(_ref()) is None


def _ref(*, broker_account_id: int = 1, broker_id: str = "zerodha") -> BrokerAccountRef:
    return BrokerAccountRef(broker_account_id=broker_account_id, broker_id=broker_id)


class TestEmergeSymbolNormalisation:
    """M61: Kite suffixes an NSE Emerge symbol with its series; NSE's register does not.

    The bug this pins shipped silently. `instrument.symbol` comes from the NSE register (`AGUL`)
    and from the bhavcopy (`AGUL`), but Kite calls the same share `AGUL-SM`. `_merge` keys on
    symbol, so the Kite row and the register row never met: 578 SME instruments carried 0
    `kite_token`s, and every Kite-sourced path — bars, deep history, holdings sync — was blind to
    them while looking like it worked.
    """

    def _record(
        self,
        configured_settings: ProviderSettings,
        stored_token: AccessTokenStore,
        tradingsymbol: str,
        exchange: str = "NSE",
    ) -> InstrumentRecord:
        client = FakeKiteClient(
            instruments=[
                {
                    "tradingsymbol": tradingsymbol,
                    "name": tradingsymbol,
                    "instrument_type": "EQ",
                    "segment": exchange,
                    "instrument_token": 5226241,
                    "lot_size": 1000,
                    "exchange": exchange,
                }
            ]
        )
        provider = build_provider(configured_settings, client, stored_token)
        return provider.list_instruments()[0]

    @pytest.mark.parametrize(
        ("kite_symbol", "expected_symbol", "expected_series"),
        [
            ("SHEETAL-SM", "SHEETAL", "SM"),
            ("TANKUP-ST", "TANKUP", "ST"),
            ("SOMENAME-SZ", "SOMENAME", "SZ"),
            # The register's own symbol already contains a hyphen; only the series comes off.
            ("RCDL-RE-ST", "RCDL-RE", "ST"),
        ],
    )
    def test_the_series_suffix_is_split_off(
        self,
        configured_settings: ProviderSettings,
        stored_token: AccessTokenStore,
        kite_symbol: str,
        expected_symbol: str,
        expected_series: str,
    ) -> None:
        record = self._record(configured_settings, stored_token, kite_symbol)
        assert record.symbol == expected_symbol
        assert record.series == expected_series
        # The whole point: the token must survive onto the row the register will merge with.
        assert record.kite_token == 5226241

    def test_a_main_board_symbol_is_untouched(
        self, configured_settings: ProviderSettings, stored_token: AccessTokenStore
    ) -> None:
        record = self._record(configured_settings, stored_token, "RELIANCE")
        assert record.symbol == "RELIANCE"
        assert record.series is None

    def test_a_rights_entitlement_is_not_treated_as_a_series(
        self, configured_settings: ProviderSettings, stored_token: AccessTokenStore
    ) -> None:
        """`-RE` is a different instrument from the share, not the share under another name."""
        record = self._record(configured_settings, stored_token, "RCDL-RE")
        assert record.symbol == "RCDL-RE"
        assert record.series is None

    def test_the_suffix_is_only_stripped_on_nse(
        self, configured_settings: ProviderSettings, stored_token: AccessTokenStore
    ) -> None:
        """Emerge is an NSE platform. A BSE symbol ending in those letters is just a symbol."""
        record = self._record(configured_settings, stored_token, "THING-SM", exchange="BSE")
        assert record.symbol == "THING-SM"
        assert record.series is None

    def test_kite_supplies_the_lot_size_the_emerge_register_omits(
        self, configured_settings: ProviderSettings, stored_token: AccessTokenStore
    ) -> None:
        """SME trades in lots and `SME_EQUITY_L.csv` has no MARKET_LOT column. Kite does."""
        record = self._record(configured_settings, stored_token, "SHEETAL-SM")
        assert record.lot_size == 1000
