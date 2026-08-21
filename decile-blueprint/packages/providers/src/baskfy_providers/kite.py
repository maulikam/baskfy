"""KiteProvider — the ``BarsProvider`` port over Zerodha Kite Connect (Prompt 2 deliverable 2).

docs/09 §"Kite specifics" is the whole specification for this module:

* the access token is daily, stored encrypted, and its expiry must alert loudly;
* the rate limit is ~3 req/s and the limiter is shared across workers via Redis;
* day-interval history is capped per request, so backfills chunk into <= 2000-day slices;
* **Kite returns unadjusted OHLC.** Everything from here is raw. The adjusted series is derived
  by the pipeline's own adjustment step, never by a provider.

docs/02 §"Why Kite ... and why it is not sufficient alone" adds the negative space: no index
constituents, no PE/PB, no corporate-action calendar. Those belong to NSEProvider, and this class
does not pretend to offer them.

Exception translation
---------------------
kiteconnect raises its own hierarchy. It is mapped onto ours at this boundary so that nothing
downstream has to import a vendor exception to decide whether to retry:

    TokenException      -> AccessTokenExpired    (loud; never retried)
    NetworkException    -> UpstreamUnavailable   (retried)
    DataException       -> UpstreamUnavailable   (retried; it signals an upstream fault)
    PermissionException -> ProviderUnavailable   (the API key lacks historical-data access)
    InputException      -> UnexpectedPayload     (our request was wrong; retrying will not help)
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Protocol

import polars as pl
from kiteconnect import KiteConnect
from kiteconnect import exceptions as kite_exceptions

from baskfy_providers.errors import (
    AccessTokenExpired,
    CredentialsMissing,
    ProviderUnavailable,
    RateLimited,
    UnexpectedPayload,
    UpstreamUnavailable,
)
from baskfy_providers.ports import BARS_CAPABILITIES, Capability, ProviderHealth
from baskfy_providers.ratelimit import RateLimiter
from baskfy_providers.records import (
    DAILY_BARS_SCHEMA,
    InstrumentRecord,
    conform,
    empty_frame,
)
from baskfy_providers.retry import RetryHooks, RetryPolicy, call_with_retry
from baskfy_providers.settings import ProviderSettings
from baskfy_providers.tokens import AccessTokenStore

PROVIDER_NAME: Final = "kite"

#: Kite's `day` interval history cap. docs/09: "chunk backfills into <= 2000-day slices".
DEFAULT_MAX_DAYS_PER_REQUEST: Final = 2000

#: Kite's instrument dump marks equities as "EQ" under segment "NSE"; everything else we care
#: about is an ETF or an index. docs/04 constrains instrument_type to EQ | ETF | INDEX.
_INDEX_SEGMENT: Final = "INDICES"


class KiteClientLike(Protocol):
    """The slice of ``kiteconnect.KiteConnect`` this adapter uses.

    Depending on a Protocol rather than the concrete class is what lets the tests exercise
    retry, chunking and error translation with zero network calls (Prompt 2 acceptance
    criterion 1) while production still passes the real client.
    """

    def set_access_token(self, access_token: str) -> None: ...

    def instruments(self, exchange: str | None = None) -> list[dict[str, object]]: ...

    def historical_data(
        self,
        instrument_token: int,
        from_date: dt.date | dt.datetime | str,
        to_date: dt.date | dt.datetime | str,
        interval: str,
    ) -> list[dict[str, object]]: ...


def _default_client_factory(api_key: str) -> KiteClientLike:
    # kiteconnect ships no type information, so the constructor is untyped to mypy. The explicit
    # annotation is where we take responsibility for it satisfying the Protocol.
    client: KiteClientLike = KiteConnect(api_key=api_key)
    return client


@dataclass(frozen=True, slots=True)
class KiteRuntime:
    """The collaborators KiteProvider is wired with.

    Bundled rather than passed loose so that the seams the tests need — a fake client, a driven
    clock, an injected limiter — do not turn the constructor into a parameter list nobody reads.
    ``rate_limiter`` is deliberately not defaulted: an unthrottled Kite client is a way to lose
    the API key, so it must be an explicit choice.
    """

    rate_limiter: RateLimiter | None = None
    client_factory: Callable[[str], KiteClientLike] = _default_client_factory
    token_store: AccessTokenStore | None = None
    retry_policy: RetryPolicy | None = None
    retry_hooks: RetryHooks | None = None


class KiteProvider:
    """``BarsProvider`` backed by Kite Connect."""

    def __init__(self, settings: ProviderSettings, runtime: KiteRuntime | None = None) -> None:
        wiring = runtime or KiteRuntime()
        self._settings = settings
        self._client_factory = wiring.client_factory
        self._token_store = wiring.token_store or AccessTokenStore(
            settings.kite_token_path, settings.kite_token_encryption_key
        )
        self._rate_limiter = wiring.rate_limiter
        self._retry_policy = wiring.retry_policy or RetryPolicy(
            max_attempts=settings.provider_max_attempts,
            base_seconds=settings.provider_backoff_base_seconds,
            max_seconds=settings.provider_backoff_max_seconds,
        )
        self._retry_hooks = wiring.retry_hooks
        self._client: KiteClientLike | None = None

    # --- HealthReporting ------------------------------------------------

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    def capabilities(self) -> frozenset[Capability]:
        return BARS_CAPABILITIES

    def check(self) -> ProviderHealth:
        """Report readiness from configuration alone. Never raises, never touches the network."""
        problems: list[str] = []
        if not self._settings.kite_api_key:
            problems.append("BASKFY_KITE_API_KEY is not set")
        if not self._settings.kite_api_secret:
            problems.append("BASKFY_KITE_API_SECRET is not set")
        if not self._settings.token_encryption_configured():
            problems.append("BASKFY_KITE_TOKEN_ENCRYPTION_KEY is not set")
        elif not self._token_store.exists():
            problems.append(f"no encrypted access token at {self._token_store.path}")
        else:
            try:
                self._token_store.require_fresh()
            except AccessTokenExpired as exc:
                problems.append(str(exc))
            except (CredentialsMissing, UnexpectedPayload) as exc:
                problems.append(str(exc))
        if self._rate_limiter is None:
            problems.append("no shared rate limiter configured; refusing to call Kite unthrottled")

        if problems:
            return ProviderHealth(
                name=self.name,
                available=False,
                capabilities=self.capabilities(),
                detail="; ".join(problems),
            )
        return ProviderHealth(
            name=self.name,
            available=True,
            capabilities=self.capabilities(),
            detail=f"access token valid, {self._settings.kite_rate_limit_per_second:g} req/s",
        )

    # --- BarsProvider ---------------------------------------------------

    def list_instruments(self) -> list[InstrumentRecord]:
        """The Kite instrument dump, narrowed to NSE and mapped onto ``InstrumentRecord``."""
        raw = self._call(lambda client: client.instruments("NSE"))
        records: list[InstrumentRecord] = []
        for row in raw:
            record = _to_instrument_record(row)
            if record is not None:
                records.append(record)
        return records

    def daily_bars(self, token: int, start: dt.date, end: dt.date) -> pl.DataFrame:
        """Raw daily candles for one instrument, chunked to respect the day-interval cap.

        Returns a frame conforming to ``DAILY_BARS_SCHEMA``. An instrument with no history in the
        window yields an empty frame with that same schema, not an error — a young listing simply
        has no bars, and docs/05 requires that to become NULL factors rather than a failure.
        """
        if end < start:
            raise ValueError(f"end {end} precedes start {start}")

        symbol = str(token)
        frames = [
            self._fetch_chunk(token, symbol, chunk_start, chunk_end)
            for chunk_start, chunk_end in self.chunk_windows(start, end)
        ]
        populated = [frame for frame in frames if frame.height > 0]
        if not populated:
            return empty_frame(DAILY_BARS_SCHEMA)
        # Chunk boundaries are inclusive on both ends, so a bar can appear twice where two
        # windows meet. Dedupe rather than trusting arithmetic that a future cap change breaks.
        combined = pl.concat(populated, how="vertical")
        return combined.unique(subset=["date"], keep="first").sort("date")

    def chunk_windows(self, start: dt.date, end: dt.date) -> Iterator[tuple[dt.date, dt.date]]:
        """Split ``[start, end]`` into slices no longer than the day-interval cap.

        Exposed rather than private because Prompt 3's resumable backfill needs the same
        boundaries to drive ``ingest_cursor``, and two implementations of this would drift.
        """
        span = dt.timedelta(days=self._settings.kite_max_days_per_request - 1)
        cursor = start
        while cursor <= end:
            chunk_end = min(cursor + span, end)
            yield cursor, chunk_end
            cursor = chunk_end + dt.timedelta(days=1)

    # --- internals ------------------------------------------------------

    def _fetch_chunk(self, token: int, symbol: str, start: dt.date, end: dt.date) -> pl.DataFrame:
        candles = self._call(
            lambda client: client.historical_data(token, start, end, "day"),
        )
        return _candles_to_frame(candles, symbol)

    def _call[T](self, operation: Callable[[KiteClientLike], T]) -> T:
        """Rate limit, then run ``operation`` under the retry policy, translating exceptions."""

        def attempt() -> T:
            client = self._authenticated_client()
            self._throttle()
            try:
                return operation(client)
            except Exception as exc:  # translated immediately; never swallowed
                raise _translate(exc) from exc

        return call_with_retry(
            attempt, self._retry_policy, provider=self.name, hooks=self._retry_hooks
        )

    def _throttle(self) -> None:
        if self._rate_limiter is None:
            raise ProviderUnavailable(
                "no shared rate limiter configured. Kite allows ~3 req/s across all workers "
                "(docs/09); calling it unthrottled risks the API key.",
                provider=self.name,
            )
        self._rate_limiter.acquire()

    def _authenticated_client(self) -> KiteClientLike:
        """Build the client once, and re-assert token freshness on every call.

        Freshness is re-checked per call rather than per client because a backfill can outlive
        the token: a run that starts at 19:00 and is still going after midnight must fail loudly
        at that moment, not keep making calls with a token the broker has already invalidated.
        """
        if not self._settings.kite_api_key:
            raise CredentialsMissing("BASKFY_KITE_API_KEY is not set", provider=self.name)
        token = self._token_store.require_fresh()
        if self._client is None:
            self._client = self._client_factory(self._settings.kite_api_key)
        self._client.set_access_token(token.value)
        return self._client


def _rate_limited_or_unavailable(exc: Exception) -> Exception:
    """Kite signals throttling through NetworkException rather than a distinct type."""
    message = str(exc).lower()
    if "too many" in message or "429" in message:
        return RateLimited(f"Kite rate-limited the request: {exc}", provider=PROVIDER_NAME)
    return UpstreamUnavailable(f"Kite network failure: {exc}", provider=PROVIDER_NAME)


#: Checked in order; the first matching entry wins, so subclasses precede their bases.
_TRANSLATIONS: Final[tuple[tuple[type[Exception], Callable[[Exception], Exception]], ...]] = (
    (
        kite_exceptions.TokenException,
        lambda exc: AccessTokenExpired(f"Kite rejected the access token: {exc}"),
    ),
    (
        kite_exceptions.PermissionException,
        lambda exc: ProviderUnavailable(
            f"the Kite API key lacks permission for this call: {exc}", provider=PROVIDER_NAME
        ),
    ),
    (
        kite_exceptions.InputException,
        lambda exc: UnexpectedPayload(
            f"Kite rejected the request as malformed: {exc}", provider=PROVIDER_NAME
        ),
    ),
    (kite_exceptions.NetworkException, _rate_limited_or_unavailable),
    (
        kite_exceptions.DataException,
        lambda exc: UpstreamUnavailable(
            f"Kite returned a bad response: {exc}", provider=PROVIDER_NAME
        ),
    ),
    (
        kite_exceptions.KiteException,
        lambda exc: UpstreamUnavailable(f"Kite failed: {exc}", provider=PROVIDER_NAME),
    ),
    (
        TimeoutError,
        lambda exc: UpstreamUnavailable(
            f"connection to Kite failed: {exc}", provider=PROVIDER_NAME
        ),
    ),
    (
        ConnectionError,
        lambda exc: UpstreamUnavailable(
            f"connection to Kite failed: {exc}", provider=PROVIDER_NAME
        ),
    ),
)


def _translate(exc: Exception) -> Exception:
    """Map a kiteconnect exception onto ours. Anything unrecognised is passed through intact."""
    for vendor_type, build in _TRANSLATIONS:
        if isinstance(exc, vendor_type):
            return build(exc)
    return exc


def _to_instrument_record(row: dict[str, object]) -> InstrumentRecord | None:
    """Map one Kite instrument-dump row. Returns ``None`` for rows outside our universe."""
    symbol = _text(row.get("tradingsymbol"))
    if not symbol:
        return None
    segment = _text(row.get("segment")) or ""
    instrument_type_raw = _text(row.get("instrument_type")) or ""

    if segment.upper() == _INDEX_SEGMENT:
        instrument_type = "INDEX"
    elif instrument_type_raw.upper() == "EQ":
        instrument_type = "EQ"
    else:
        # Futures, options and everything else Kite carries are out of scope (docs/01: the
        # product screens NSE equities and ETFs).
        return None

    return InstrumentRecord(
        symbol=symbol,
        name=_text(row.get("name")) or symbol,
        instrument_type=instrument_type,
        series=None,
        kite_token=_int(row.get("instrument_token")),
        lot_size=_int(row.get("lot_size")),
        exchange=_text(row.get("exchange")) or "NSE",
    )


def _candles_to_frame(candles: Sequence[dict[str, object]], symbol: str) -> pl.DataFrame:
    if not candles:
        return empty_frame(DAILY_BARS_SCHEMA)

    rows: list[dict[str, object]] = []
    for candle in candles:
        rows.append(
            {
                "symbol": symbol,
                "date": _date(candle.get("date")),
                "open": _decimal(candle.get("open")),
                "high": _decimal(candle.get("high")),
                "low": _decimal(candle.get("low")),
                "close": _decimal(candle.get("close")),
                "volume": _int(candle.get("volume")) or 0,
                "source": PROVIDER_NAME,
            }
        )
    return conform(pl.DataFrame(rows, strict=False), DAILY_BARS_SCHEMA)


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int(value: object) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        return int(float(value))
    raise UnexpectedPayload(f"cannot read {value!r} as an integer", provider=PROVIDER_NAME)


def _decimal(value: object) -> Decimal | None:
    """Prices become Decimal at the boundary, never float (docs/04: numeric, never float)."""
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float, str)):
        return Decimal(str(value))
    raise UnexpectedPayload(f"cannot read {value!r} as a price", provider=PROVIDER_NAME)


def _date(value: object) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        return dt.datetime.fromisoformat(value).date()
    raise UnexpectedPayload(f"cannot read {value!r} as a date", provider=PROVIDER_NAME)
