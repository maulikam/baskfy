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
from typing import Final, Protocol, runtime_checkable

import polars as pl
from kiteconnect import KiteConnect
from kiteconnect import exceptions as kite_exceptions

from baskfy_core.tradebook import TradeFill, TradeSide
from baskfy_providers.errors import (
    AccessTokenExpired,
    CredentialsMissing,
    ProviderUnavailable,
    RateLimited,
    UnexpectedPayload,
    UpstreamUnavailable,
)
from baskfy_providers.ports import (
    BARS_CAPABILITIES,
    HOLDINGS_CAPABILITIES,
    Capability,
    ProviderHealth,
)
from baskfy_providers.ratelimit import RateLimiter
from baskfy_providers.records import (
    DAILY_BARS_SCHEMA,
    BasketMarginRecord,
    BrokerAccountRef,
    BrokerHoldingRecord,
    DepthLevelRecord,
    InstrumentRecord,
    MarginLegRecord,
    MinuteBarRecord,
    OptionContractRecord,
    OptionQuoteRecord,
    QuoteRecord,
    conform,
    empty_frame,
)
from baskfy_providers.retry import RetryHooks, RetryPolicy, call_with_retry
from baskfy_providers.settings import ProviderSettings
from baskfy_providers.tokens import AccessTokenStore

PROVIDER_NAME: Final = "kite"

#: Kite's `day` interval history cap. docs/09: "chunk backfills into <= 2000-day slices".
DEFAULT_MAX_DAYS_PER_REQUEST: Final = 2000

#: Kite's ``GET /quote`` accepts up to 500 instruments per call (docs/swing/01 §"Pre-open":
#: "≤ 6 calls of 500" for the liquid universe). A larger batch is refused by Kite with a 400,
#: which the retry policy would then repeat — so the cap is enforced here, before the call.
QUOTE_BATCH_SIZE: Final = 500

#: Kite's ``minute`` interval history cap per request, in calendar days (Kite Connect's historical
#: API documentation: 60 days for ``minute``). A wider window is refused with an InputException,
#: which retrying would not cure — so the chunking below is the contract, not an optimisation.
MINUTE_MAX_DAYS_PER_REQUEST: Final = 60

#: The exchange's clock. Kite stamps candles and quotes in IST; a naive stamp is read as IST.
_IST: Final = dt.timezone(dt.timedelta(hours=5, minutes=30))

#: The catalog id (``baskfy_core.broker_connections``) this adapter is the adapter *for*. A ref
#: naming any other broker is refused rather than served, because "the holdings provider" and
#: "this user's broker" are not the same thing the moment a second broker exists.
KITE_BROKER_ID: Final = "zerodha"

#: Kite's margins response nests the number we want. ``net`` is the withdrawable + utilised
#: balance for the segment, which is what the user sees as "cash" on their own dashboard.
_EQUITY_SEGMENT: Final = "equity"

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

    def holdings(self) -> list[dict[str, object]]: ...

    def margins(self, segment: str | None = None) -> dict[str, object]: ...

    def quote(self, *instruments: str) -> dict[str, dict[str, object]]: ...

    def trades(self) -> list[dict[str, object]]: ...


@runtime_checkable
class BasketMarginClient(Protocol):
    """The one extra verb :meth:`KiteProvider.basket_order_margins` needs (OP3).

    Kept out of :class:`KiteClientLike` so that every existing test double stays a valid client;
    the real ``KiteConnect`` has it, and a client without it is refused, not guessed around.
    """

    def basket_order_margins(
        self,
        params: list[dict[str, object]],
        consider_positions: bool = True,
        mode: str | None = None,
    ) -> dict[str, object]: ...


def _default_client_factory(api_key: str, *, timeout: float = 30.0) -> KiteClientLike:
    # kiteconnect ships no type information, so the constructor is untyped to mypy. The explicit
    # annotation is where we take responsibility for it satisfying the Protocol.
    # AF 3.11: pass timeout explicitly — the SDK's 7 s default is not a product decision.
    client: KiteClientLike = KiteConnect(api_key=api_key, timeout=timeout)
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
    #: The ``broker_account.id`` the configured access token belongs to, when it is known.
    #:
    #: P4.2 (per-user encrypted OAuth tokens) is not built, so this adapter holds exactly one
    #: token and cannot discover whose it is without a profile round trip. Setting this makes
    #: the deployment's own knowledge enforceable: a holdings read for any other account is
    #: refused instead of quietly answered with the token-holder's positions. Left ``None`` on
    #: the single-tenant founder box, where there is only one account and nothing to confuse.
    holdings_account_id: int | None = None


class KiteProvider:
    """``BarsProvider`` backed by Kite Connect."""

    def __init__(self, settings: ProviderSettings, runtime: KiteRuntime | None = None) -> None:
        wiring = runtime or KiteRuntime()
        self._settings = settings
        # Bind the configured timeout into the default factory without changing the Protocol
        # signature tests inject against.
        self._client_factory: Callable[[str], KiteClientLike]
        if wiring.client_factory is _default_client_factory:
            timeout = settings.kite_request_timeout_seconds

            def factory(api_key: str) -> KiteClientLike:
                return _default_client_factory(api_key, timeout=timeout)

            self._client_factory = factory
        else:
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
        self._holdings_account_id = wiring.holdings_account_id
        self._client: KiteClientLike | None = None

    # --- HealthReporting ------------------------------------------------

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @property
    def rate_limiter(self) -> RateLimiter | None:
        """Which clock this adapter's reads wait on, or ``None`` when Redis was unreachable.

        Public because *which lane a provider was built for* is a property worth being able to
        assert and to report (M85): the difference between the bulk lane and the interactive one
        is invisible at the call site and decides whether a login-time read waits behind an hour
        of backfill. Read-only — nothing can swap a limiter after construction.
        """
        return self._rate_limiter

    def capabilities(self) -> frozenset[Capability]:
        """Bars, and the read-only broker ledger. Never an order verb — law 2."""
        return BARS_CAPABILITIES | HOLDINGS_CAPABILITIES

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

    #: Our universe slug -> Kite's own index tradingsymbol. Kite abbreviates; nothing derives one
    #: name from the other, so the two the swing gate can ask about are written out. A slug that
    #: is not here simply has no live level and the gate falls back to published closes, which is
    #: the pre-9-Sep-2026 behaviour and never wrong, only late.
    INDEX_QUOTE_SYMBOL: Final[dict[str, str]] = {
        "nifty-mid-small-400": "NIFTY MIDSML 400",
        "nifty-500": "NIFTY 500",
    }

    def index_level(self, slug: str) -> Decimal | None:
        """Today's live level for a benchmark index, or ``None`` if it cannot be quoted.

        WHY THE GATE NEEDS THIS (Maulik, 9 Sep 2026). `index_snapshot_daily` holds only PUBLISHED
        sessions, so during the day its newest row is yesterday's. A live scan recomputed breadth
        from live bars but read the index rule off yesterday's averages — so the half of the gate
        that actually decides RED or GREEN could not move until the nightly ran. He wants the
        verdict checked against the live tape when he connects Kite, and this is the missing
        input.

        ``None`` rather than an exception for an unmapped slug, a missing quote or a malformed
        one: without a live level the gate reads published closes, which is exactly what it did
        before and is never wrong, only late. A live gate that raises would be worse than a
        late one.
        """
        symbol = self.INDEX_QUOTE_SYMBOL.get(slug)
        if symbol is None:
            return None
        key = f"NSE:{symbol}"
        payload = self._call(lambda client: client.quote(key))
        row = payload.get(key) if isinstance(payload, dict) else None
        if not isinstance(row, dict):
            return None
        last = row.get("last_price")
        if last is None:
            return None
        try:
            level = Decimal(str(last))
        except (ArithmeticError, ValueError):
            return None
        return level if level > 0 else None

    def fno_underlyings(self) -> list[str]:
        """Every equity that has a futures contract — the ``nifty-fno`` universe (9 Sep 2026).

        WHY THIS EXISTS. `nifty-fno` screened to nothing, on every run, because it had no
        membership source at all: `quality.NO_MEMBERSHIP_SOURCE` listed it as needing "NSE's F&O
        constituent file, which no provider fetches", so `index_member_daily` held zero rows for
        it and the screen correctly returned an empty set for an empty universe.

        No such file is needed. Kite's own instrument dump carries the F&O segment, and every
        futures row names its underlying — so the set of distinct ``name`` values across NFO
        futures IS the F&O universe, from a source this deployment already speaks to.

        **Futures, not options.** Every F&O underlying has a futures contract, and one per expiry
        rather than the hundreds of strikes an option chain adds; filtering to ``FUT`` is both the
        complete answer and the cheap one. Deduplicated and sorted so a re-run writes identical
        rows (house rule 7).

        One HTTP call, on the same rate-limited path as everything else. `list_instruments` asks
        for ``"NSE"``; this asks for ``"NFO"``, and neither is a per-instrument fetch.
        """
        raw = self._call(lambda client: client.instruments("NFO"))
        underlyings: set[str] = set()
        for row in raw:
            if (_text(row.get("instrument_type")) or "").upper() != "FUT":
                continue
            name = _text(row.get("name"))
            if name:
                underlyings.add(name.upper())
        return sorted(underlyings)

    def option_contracts(self, underlying: str) -> list[OptionContractRecord]:
        """Every listed CE/PE contract on ``underlying`` from the NFO dump (``docs/options/03`` §1).

        One HTTP call, on the same rate-limited path as :meth:`fno_underlyings`. Filtered to
        ``name == underlying`` and ``instrument_type in {CE, PE}``; a row missing a fact the
        options calendar needs (expiry, strike, lot size, tick size) is skipped rather than
        guessed, because a contract with an invented lot size would size a plan wrong. Sorted by
        token so a re-run writes identical rows (house rule 7). Read-only: this never reaches an
        order path.
        """
        wanted = underlying.upper().strip()
        raw = self._call(lambda client: client.instruments("NFO"))
        out: list[OptionContractRecord] = []
        for row in raw:
            record = _to_option_contract(row, wanted)
            if record is not None:
                out.append(record)
        return sorted(out, key=lambda r: r.instrument_token)

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

    # --- HoldingsProvider -----------------------------------------------

    def broker_holdings(self, account: BrokerAccountRef) -> list[BrokerHoldingRecord]:
        """``GET /portfolio/holdings``, mapped onto :class:`BrokerHoldingRecord`.

        Read-only, and structurally incapable of being anything else: it calls
        ``kiteconnect``'s ``holdings()`` and nothing on this class can place an order (law 2 —
        the order path is ``packages/execution``, and this module does not import it).

        A row Kite sends that we cannot read is **skipped, counted by its absence, and never
        guessed at**. That is the one place this method is lenient, and it is deliberate: a
        single malformed row (a new instrument class, a null where a number was) must not cost
        the user the sync of every other position they own. The sync above it re-derives its own
        totals from what it received, so a skipped row shows up as a position the broker no
        longer reports — a question — rather than as silence.
        """
        self._require_own_account(account)
        raw = self._call(lambda client: client.holdings())
        records: list[BrokerHoldingRecord] = []
        for row in raw:
            record = _to_holding_record(row)
            if record is not None:
                records.append(record)
        return records

    def broker_cash(self, account: BrokerAccountRef) -> Decimal | None:
        """The equity segment's net balance, as Decimal. ``None`` when Kite does not report one.

        ``None`` rather than zero when the payload has no readable figure, because §4.4's cash
        bucket is part of consolidated net worth: writing a zero we inferred from a missing key
        would take real money off the user's screen and still balance.
        """
        self._require_own_account(account)
        payload = self._call(lambda client: client.margins(_EQUITY_SEGMENT))
        return _equity_net_cash(payload)

    def broker_trades(self, account: BrokerAccountRef) -> list[TradeFill]:
        """``GET /trades`` — today's executions, and only today's: Kite takes no date and flushes
        the list nightly (NEEDS-MAULIK §32). Read-only, like every method here (law 2).

        Strict where :meth:`broker_holdings` is lenient. A skipped trade would leave a holding
        whose trades no longer add up, so an equity row this cannot read refuses the whole read
        and the day can be captured again, rather than recorded with a hole in it. Derivative and
        currency rows are not equity and are left out.
        """
        self._require_own_account(account)
        raw = self._call(lambda client: client.trades())
        return [fill for row in raw if (fill := _to_trade_fill(row)) is not None]

    # --- Quotes (SW6) ---------------------------------------------------------

    def quotes(self, symbols: Sequence[str], *, exchange: str = "NSE") -> list[QuoteRecord]:
        """``GET /quote`` for ``symbols``, in batches of at most :data:`QUOTE_BATCH_SIZE`.

        Every batch is one throttled call — so a 2,000-name universe is four tokens from the
        shared limiter, never one unthrottled burst. Read-only, like everything on this class:
        a quote is a price, and nothing here can act on one (law 2).

        A symbol Kite does not answer for is absent from the result rather than invented; a
        row that cannot be read is skipped the way ``broker_holdings`` skips one, because one
        malformed quote must not cost the scan the other 499.
        """
        wanted = [symbol for symbol in symbols if symbol]
        records: list[QuoteRecord] = []
        for start in range(0, len(wanted), QUOTE_BATCH_SIZE):
            batch = [f"{exchange}:{symbol}" for symbol in wanted[start : start + QUOTE_BATCH_SIZE]]
            payload = self._call(_quote_call(batch))
            for key, row in payload.items():
                record = _to_quote_record(key, row)
                if record is not None:
                    records.append(record)
        return records

    # --- Options reads (OP3, docs/options/06) ------------------------------------

    def option_quotes(self, keys: Sequence[str]) -> list[OptionQuoteRecord]:
        """``GET /quote`` for ``EXCHANGE:SYMBOL`` keys, **with** depth, OI and timestamps.

        Keys are passed whole so one call can carry NFO contracts and the ``NSE:NIFTY 50`` spot
        together — the collector's "one ``quote()`` call a minute" (``docs/options/03`` §5).
        Batched at :data:`QUOTE_BATCH_SIZE`, each batch one throttled call, exactly like
        :meth:`quotes`. A key Kite does not answer for is absent; an unreadable row is skipped.
        Read-only (law 2).
        """
        wanted = list(dict.fromkeys(key for key in keys if key))
        records: list[OptionQuoteRecord] = []
        for start in range(0, len(wanted), QUOTE_BATCH_SIZE):
            batch = wanted[start : start + QUOTE_BATCH_SIZE]
            payload = self._call(_quote_call(batch))
            for key, row in payload.items():
                record = _to_option_quote(key, row)
                if record is not None:
                    records.append(record)
        return records

    def minute_windows(self, start: dt.date, end: dt.date) -> Iterator[tuple[dt.date, dt.date]]:
        """``[start, end]`` in slices of at most :data:`MINUTE_MAX_DAYS_PER_REQUEST` days."""
        if end < start:
            raise ValueError(f"end {end} precedes start {start}")
        span = dt.timedelta(days=MINUTE_MAX_DAYS_PER_REQUEST - 1)
        cursor = start
        while cursor <= end:
            chunk_end = min(cursor + span, end)
            yield cursor, chunk_end
            cursor = chunk_end + dt.timedelta(days=1)

    def minute_bars(
        self,
        token: int,
        start: dt.datetime | dt.date,
        end: dt.datetime | dt.date,
    ) -> list[MinuteBarRecord]:
        """One-minute candles for ``token`` over ``[start, end]``, chunked per Kite's cap.

        A ``date`` means the whole day (00:00 to 23:59:59 IST); a ``datetime`` is read in IST.
        Every chunk is one throttled call. Bars are de-duplicated on ``ts`` (chunks meet at a day
        boundary) and returned in time order; an empty window is an empty list, not an error — an
        index before Kite's history starts simply has no bars.
        """
        lo = _ist_wall(start, end_of_day=False)
        hi = _ist_wall(end, end_of_day=True)
        if hi < lo:
            raise ValueError(f"end {hi} precedes start {lo}")
        by_ts: dict[dt.datetime, MinuteBarRecord] = {}
        for day_lo, day_hi in self.minute_windows(lo.date(), hi.date()):
            chunk_lo = max(lo, dt.datetime.combine(day_lo, dt.time(0, 0)))
            chunk_hi = min(hi, dt.datetime.combine(day_hi, dt.time(23, 59, 59)))
            candles = self._call(_minute_call(token, chunk_lo, chunk_hi))
            for candle in candles:
                bar = _to_minute_bar(candle)
                if bar is not None:
                    by_ts.setdefault(bar.ts, bar)
        return [by_ts[ts] for ts in sorted(by_ts)]

    def basket_order_margins(
        self, legs: Sequence[MarginLegRecord], *, consider_positions: bool = False
    ) -> BasketMarginRecord:
        """Kite's ``/margins/basket`` for ``legs`` — **a calculation, never an order** (OP3).

        ``consider_positions`` defaults to ``False`` so the answer is the basket's own requirement
        and not a function of whatever the account holds (Track C §8: the options book never reads
        another book's positions). Plans use it as a ceiling, never to size (Track C §10).
        """
        if not legs:
            raise ValueError("a basket margin needs at least one leg")
        params: list[dict[str, object]] = [
            {
                "exchange": leg.exchange,
                "tradingsymbol": leg.tradingsymbol,
                "transaction_type": leg.transaction_type,
                "variety": leg.variety,
                "product": leg.product,
                "order_type": leg.order_type,
                "quantity": leg.quantity,
                "price": float(leg.price),
            }
            for leg in legs
        ]

        def call(client: KiteClientLike) -> dict[str, object]:
            if not isinstance(client, BasketMarginClient):
                raise ProviderUnavailable(
                    "this Kite client has no basket_order_margins", provider=PROVIDER_NAME
                )
            return client.basket_order_margins(params, consider_positions, None)

        return _to_basket_margin(self._call(call), len(params))

    def margins_shape(self) -> dict[str, object]:
        """``margins()`` reduced to segment and field names with their types — **no figures**.

        OP3's probe records what the NFO margin payload looks like (OP0 §4 (d)); an account's
        balances have no business in a probe report or a log, so they never leave this method.
        """
        return _margins_shape(self._call(lambda client: client.margins()))

    def _require_own_account(self, account: BrokerAccountRef) -> None:
        """Refuse a read for an account this adapter's credentials do not belong to.

        The read-side twin of the two laws' multi-tenant clause. Two refusals, both cheap:
        a ref naming another broker (this adapter is Zerodha's, and Upstox holdings fetched
        through a Kite token would be nonsense), and — where the deployment has told us which
        account the token belongs to — a ref naming a different one.
        """
        if account.broker_id != KITE_BROKER_ID:
            raise ProviderUnavailable(
                f"this adapter serves {KITE_BROKER_ID!r} and was asked for "
                f"{account.broker_id!r}; a broker's holdings are not fetchable with another "
                "broker's session",
                provider=self.name,
            )
        if (
            self._holdings_account_id is not None
            and account.broker_account_id != self._holdings_account_id
        ):
            raise ProviderUnavailable(
                f"the configured Kite session belongs to broker account "
                f"{self._holdings_account_id} and holdings were requested for "
                f"{account.broker_account_id}; refusing rather than answering with another "
                "tenant's positions",
                provider=self.name,
            )

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


#: Kite spells an NSE Emerge symbol with its series appended — `SHEETAL-SM`, `TANKUP-ST` —
#: where NSE's own register, the bhavcopy and therefore `instrument.symbol` all use the bare
#: `SHEETAL`. Left alone, the two never meet: `_merge` keys on symbol, so every Emerge name
#: ends up as two rows, and the one the pipeline screens is the one with no `kite_token`. That
#: is exactly what happened — 578 SME instruments, 0 Kite tokens, and no Kite bars for any of
#: them (M61).
#:
#: Only these three. `-RE` (rights entitlement) and `-BE` are *not* stripped: a rights
#: entitlement is a different instrument from the share, not the same share under another name.
_SME_SUFFIXES: Final[tuple[str, ...]] = ("-SM", "-ST", "-SZ")


def _split_sme_symbol(symbol: str, exchange: str) -> tuple[str, str | None]:
    """``('SHEETAL-SM', 'NSE') -> ('SHEETAL', 'SM')``; anything else passes through unchanged.

    Guarded on the exchange because the suffix is only Kite's Emerge convention on NSE; a BSE
    symbol that happens to end in those two letters is not an SME listing.
    """
    if exchange.upper() != "NSE":
        return symbol, None
    for suffix in _SME_SUFFIXES:
        if symbol.endswith(suffix) and len(symbol) > len(suffix):
            return symbol[: -len(suffix)], suffix.lstrip("-")
    return symbol, None


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

    exchange = _text(row.get("exchange")) or "NSE"
    # Emerge names arrive suffixed; normalise so this row merges with the NSE register's.
    bare, sme_series = _split_sme_symbol(symbol, exchange)

    return InstrumentRecord(
        symbol=bare,
        name=_text(row.get("name")) or bare,
        instrument_type=instrument_type,
        # Kite publishes no series for the main board, so this stays None there and the NSE
        # register fills it in. For Emerge the suffix *is* the series, and it is the only place
        # Kite states it — worth keeping rather than rediscovering from the bhavcopy.
        series=sme_series,
        kite_token=_int(row.get("instrument_token")),
        lot_size=_int(row.get("lot_size")),
        exchange=exchange,
    )


def _to_option_contract(row: dict[str, object], underlying: str) -> OptionContractRecord | None:
    """One NFO dump row as an option contract on ``underlying``, or ``None``."""
    if (_text(row.get("name")) or "").upper() != underlying:
        return None
    option_type = (_text(row.get("instrument_type")) or "").upper()
    if option_type not in ("CE", "PE"):
        return None
    token = _int(row.get("instrument_token"))
    symbol = _text(row.get("tradingsymbol"))
    expiry_raw = row.get("expiry")
    strike = _decimal(row.get("strike"))
    lot_size = _int(row.get("lot_size"))
    tick_size = _decimal(row.get("tick_size"))
    if (
        not token
        or not symbol
        or expiry_raw in (None, "")
        or not strike
        or not lot_size
        or not tick_size
    ):
        return None
    return OptionContractRecord(
        instrument_token=token,
        tradingsymbol=symbol,
        underlying=underlying,
        expiry=_date(expiry_raw),
        strike=strike,
        option_type="CE" if option_type == "CE" else "PE",
        lot_size=lot_size,
        tick_size=tick_size,
    )


def _to_holding_record(row: dict[str, object]) -> BrokerHoldingRecord | None:
    """Map one Kite holdings row. ``None`` for a row we cannot read as a position.

    ``average_price`` is passed through as Kite sends it, including a genuine zero — Kite reports
    zero for a position it has no cost basis for (an IPO allotment, a transferred holding), and
    §5.2 needs that to arrive as "unknown" rather than "free". ``_decimal`` returns ``None`` for
    an absent key, which is exactly that distinction; a zero Kite really sent stays a zero.
    """
    symbol = _text(row.get("tradingsymbol")) or _text(row.get("symbol"))
    if not symbol:
        return None
    try:
        return BrokerHoldingRecord(
            symbol=symbol,
            exchange=_text(row.get("exchange")) or "NSE",
            isin=_text(row.get("isin")),
            quantity=_quantity(row.get("quantity")),
            t1_quantity=_quantity(row.get("t1_quantity")),
            collateral_quantity=_quantity(row.get("collateral_quantity")),
            average_price=_decimal(row.get("average_price")),
            last_price=_decimal(row.get("last_price")),
            product=_text(row.get("product")) or "CNC",
        )
    except (UnexpectedPayload, ValueError):
        # Narrow on purpose (house rule 3): a payload we cannot read is skipped, a bug in our
        # own mapping still escapes. See broker_holdings' docstring for why one bad row does
        # not fail the whole fetch.
        return None


_EQUITY_EXCHANGES: Final = frozenset({"NSE", "BSE"})


def _to_trade_fill(row: dict[str, object]) -> TradeFill | None:
    """Map one Kite trade. ``None`` off NSE/BSE; raises for an equity row it cannot read."""
    exchange = (_text(row.get("exchange")) or "").upper()
    if exchange not in _EQUITY_EXCHANGES:
        return None
    symbol = _text(row.get("tradingsymbol"))
    trade_id = _text(row.get("trade_id"))
    side_raw = (_text(row.get("transaction_type")) or "").upper()
    quantity = _decimal(row.get("quantity"))
    price = _decimal(row.get("average_price"))
    when = row.get("fill_timestamp") or row.get("exchange_timestamp") or row.get("order_timestamp")
    if (
        not symbol
        or not trade_id
        or side_raw not in {"BUY", "SELL"}
        or quantity is None
        or quantity <= 0
        or price is None
        or not isinstance(when, dt.datetime)
    ):
        raise UnexpectedPayload(
            f"a trade row could not be read (trade_id={trade_id!r}, symbol={symbol!r})",
            provider=PROVIDER_NAME,
        )
    return TradeFill(
        symbol=symbol.upper(),
        exchange=exchange,
        side=TradeSide(side_raw),
        quantity=quantity,
        price=price,
        trade_date=when.date(),
        trade_id=trade_id,
        order_id=_text(row.get("order_id")),
        executed_at=when,
    )


def _quote_call(batch: list[str]) -> Callable[[KiteClientLike], dict[str, dict[str, object]]]:
    """Bind one batch to a call, so the loop above does not close over a changing name."""

    def call(client: KiteClientLike) -> dict[str, dict[str, object]]:
        return client.quote(*batch)

    return call


def _minute_call(
    token: int, start: dt.datetime, end: dt.datetime
) -> Callable[[KiteClientLike], list[dict[str, object]]]:
    """Bind one minute-history window to a call (naive IST wall-clock, as Kite reads it)."""

    def call(client: KiteClientLike) -> list[dict[str, object]]:
        return client.historical_data(token, start, end, "minute")

    return call


def _ist_wall(value: dt.datetime | dt.date, *, end_of_day: bool) -> dt.datetime:
    """A naive IST wall-clock datetime: Kite's historical API reads its bounds that way."""
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            return value
        return value.astimezone(_IST).replace(tzinfo=None)
    return dt.datetime.combine(value, dt.time(23, 59, 59) if end_of_day else dt.time(0, 0))


def _aware_ist(value: object) -> dt.datetime | None:
    """A Kite timestamp as an aware datetime; a naive one is IST (Kite's own clock)."""
    if isinstance(value, dt.datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=_IST)
    if isinstance(value, str) and value:
        try:
            parsed = dt.datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=_IST)
    return None


def _to_minute_bar(candle: dict[str, object]) -> MinuteBarRecord | None:
    """One candle → :class:`MinuteBarRecord`; ``None`` when a price or the stamp is missing."""
    ts = _aware_ist(candle.get("date"))
    prices = [_decimal(candle.get(k)) for k in ("open", "high", "low", "close")]
    if ts is None or any(p is None for p in prices):
        return None
    o, h, lo, c = (p for p in prices if p is not None)
    return MinuteBarRecord(
        ts=ts.replace(second=0, microsecond=0),
        open=o,
        high=h,
        low=lo,
        close=c,
        volume=_int(candle.get("volume")) or 0,
    )


def _depth_side(raw: object) -> tuple[DepthLevelRecord, ...]:
    """One side of Kite's ``depth``; zero-priced padding levels are dropped."""
    if not isinstance(raw, list):
        return ()
    levels: list[DepthLevelRecord] = []
    for level in raw:
        if not isinstance(level, dict):
            continue
        price = _decimal(level.get("price"))
        if price is None or price <= 0:
            continue
        levels.append(
            DepthLevelRecord(
                price=price,
                quantity=max(_int(level.get("quantity")) or 0, 0),
                orders=max(_int(level.get("orders")) or 0, 0),
            )
        )
    return tuple(levels)


def _to_option_quote(key: str, row: object) -> OptionQuoteRecord | None:
    """One ``/quote`` entry with depth → :class:`OptionQuoteRecord`, or ``None``."""
    if not isinstance(row, dict):
        return None
    exchange, _, symbol = key.partition(":")
    if not symbol or not exchange:
        return None
    depth = row.get("depth")
    sides = depth if isinstance(depth, dict) else {}
    try:
        return OptionQuoteRecord(
            symbol=symbol,
            exchange=exchange,
            instrument_token=_int(row.get("instrument_token")),
            last_price=_decimal(row.get("last_price")),
            volume=_int(row.get("volume") or row.get("volume_traded")) or 0,
            oi=_int(row.get("oi")),
            oi_day_high=_int(row.get("oi_day_high")),
            oi_day_low=_int(row.get("oi_day_low")),
            bids=_depth_side(sides.get("buy")),
            asks=_depth_side(sides.get("sell")),
            as_of=_aware_ist(row.get("timestamp")),
            last_trade_time=_aware_ist(row.get("last_trade_time")),
        )
    except (UnexpectedPayload, ValueError):
        # Narrow on purpose (house rule 3): one unreadable quote must not cost the other 124.
        return None


def _margins_shape(payload: object) -> dict[str, object]:
    """Segment -> field -> JSON type name (or a nested block's sorted keys). Never a value."""
    if not isinstance(payload, dict):
        return {"type": type(payload).__name__}
    out: dict[str, object] = {}
    for segment, body in sorted(payload.items(), key=lambda kv: str(kv[0])):
        if not isinstance(body, dict):
            out[str(segment)] = type(body).__name__
            continue
        out[str(segment)] = {
            str(key): (
                sorted(str(k) for k in value) if isinstance(value, dict) else type(value).__name__
            )
            for key, value in sorted(body.items(), key=lambda kv: str(kv[0]))
        }
    return out


def _to_basket_margin(payload: object, legs: int) -> BasketMarginRecord:
    """Kite's basket-margin response → totals and the response's shape (no account figures)."""
    if not isinstance(payload, dict):
        raise UnexpectedPayload("basket margins returned no object", provider=PROVIDER_NAME)
    shape: dict[str, list[str]] = {"top": sorted(str(k) for k in payload)}
    totals: dict[str, Decimal | None] = {}
    for block in ("initial", "final"):
        raw = payload.get(block)
        if isinstance(raw, dict):
            shape[block] = sorted(str(k) for k in raw)
            totals[block] = _decimal(raw.get("total"))
        else:
            totals[block] = None
    return BasketMarginRecord(
        initial_total=totals["initial"], final_total=totals["final"], legs=legs, shape=shape
    )


def _to_quote_record(key: str, row: object) -> QuoteRecord | None:
    """One ``/quote`` entry → :class:`QuoteRecord`, or ``None`` when it cannot be read.

    Kite keys the payload by ``EXCHANGE:SYMBOL`` and nests the day's OHLC under ``ohlc``.
    """
    if not isinstance(row, dict):
        return None
    exchange, _, symbol = key.partition(":")
    if not symbol:
        return None
    last = _decimal(row.get("last_price"))
    if last is None:
        return None
    ohlc = row.get("ohlc")
    bands = ohlc if isinstance(ohlc, dict) else {}
    stamp = row.get("timestamp") or row.get("last_trade_time")
    return QuoteRecord(
        symbol=symbol,
        exchange=exchange or "NSE",
        instrument_token=_int(row.get("instrument_token")),
        last_price=last,
        volume=_int(row.get("volume") or row.get("volume_traded")) or 0,
        prev_close=_decimal(bands.get("close")),
        open=_decimal(bands.get("open")),
        high=_decimal(bands.get("high")),
        low=_decimal(bands.get("low")),
        upper_circuit=_decimal(row.get("upper_circuit_limit")),
        lower_circuit=_decimal(row.get("lower_circuit_limit")),
        as_of=stamp if isinstance(stamp, dt.datetime) else None,
    )


def _equity_net_cash(payload: dict[str, object]) -> Decimal | None:
    """Read the equity segment's ``net`` balance out of a Kite margins response.

    Accepts either the segment payload (``margins("equity")``) or the whole envelope
    (``margins()``), because the two differ only by one level of nesting and a caller that got
    the shape wrong should still get their money rather than a silent ``None``.
    """
    segment = payload.get(_EQUITY_SEGMENT)
    if isinstance(segment, dict):
        payload = segment
    net = payload.get("net")
    if net is None:
        return None
    return _decimal(net)


def _quantity(value: object) -> Decimal:
    """A broker quantity. Absent means zero — a segment the broker did not mention holds none."""
    parsed = _decimal(value)
    return Decimal("0") if parsed is None else parsed


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
