"""NSEProvider — the ``ReferenceProvider`` port over NSE's public files (Prompt 2 deliverable 3).

docs/02 §"Why Kite ... and why it is not sufficient alone" defines the job: Kite has no index
constituents, no index PE/PB/DivYield, no corporate-action calendar, no series codes for the full
universe and no listings history. All of that comes from here.

docs/09 §"NSE specifics" defines the discipline:

* public files need a browser-like session, so cookies are primed against the site root before
  any archive request;
* they are rate-sensitive, so every request passes through a token bucket;
* **fetch once, archive the raw file, parse from the archive.** Every method below routes through
  :func:`fetch_and_archive`, so the bytes that get parsed are always the bytes on record.

What this module does *not* do is decide whether a day is usable. docs/09: "Files occasionally
publish late or malformed -> the QA gate, not the parser, decides whether the day is publishable."
So parsers raise on structural damage, and stay silent about business-level oddities.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import re
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Final, Protocol
from urllib.parse import quote

import httpx
import polars as pl

from baskfy_core.universes import UNIVERSE_BY_SLUG
from baskfy_providers.archive import RawArchive, archive_key, fetch_and_archive
from baskfy_providers.errors import (
    ProviderUnavailable,
    UnexpectedPayload,
    UpstreamUnavailable,
)
from baskfy_providers.ports import REFERENCE_CAPABILITIES, Capability, ProviderHealth
from baskfy_providers.ratelimit import RateLimiter
from baskfy_providers.records import (
    BHAVCOPY_SCHEMA,
    CatalystRecord,
    CorporateAction,
    EarningsDateRecord,
    EquityFundamental,
    IndexSnapshot,
    ListingRecord,
    conform,
)
from baskfy_providers.retry import RetryHooks, RetryPolicy, call_with_retry
from baskfy_providers.settings import ProviderSettings

PROVIDER_NAME: Final = "nse"

#: Archive "kinds" — the ``{kind}`` in docs/09's ``nse/{kind}/{date}.csv``.
KIND_BHAVCOPY: Final = "bhavcopy"
KIND_INDEX_SNAPSHOT: Final = "index-snapshot"
KIND_CORPORATE_ACTIONS: Final = "corporate-actions"
#: The windowed request is a *different question* from the un-ranged one, so it gets its own
#: archive kind. `fetch_and_archive` never re-fetches a key that exists (docs/09, "Never re-fetch
#: to re-parse"), so reusing the old kind would have kept re-parsing the truncated 20-row payloads
#: already on disk and the fix would have been a no-op. The old files stay as the record of what
#: the old code actually saw.
KIND_CORPORATE_ACTIONS_WINDOWED: Final = "corporate-actions-windowed"

#: Rows NSE returns when it ignores the window and serves its default first page.
#:
#: This exact number is the incident: twelve consecutive nightly payloads, every one of them 20
#: rows, while NSE held 87 actions for 2026-09-11 alone. A count that lands on it again means the
#: range was dropped, and an adjusted price series built on a default page is worse than a loud
#: failure. See `gates/ca-truncation.md`.
NSE_DEFAULT_PAGE_ROWS: Final = 20
KIND_LISTINGS: Final = "listings"
KIND_CONSTITUENTS: Final = "constituents"
KIND_EQUITY_FUNDAMENTALS: Final = "equity-fundamentals"
KIND_EQUITY_META: Final = "equity-meta"

#: NSE retired ``/api/quote-equity`` when the site moved to Next.js; the edge now answers it with
#: a 403 from AkamaiGHost, which reads like a bot block and is really a removed route. The quote
#: page calls this proxy instead, with a ``functionName`` naming the operation. Discovered by
#: reading the page's own chunks (DECISIONS-MERGE §T3F.1) because NSE publishes no API contract.
_GET_QUOTE_API: Final = "/api/NextApi/apiClient/GetQuoteApi"

#: Rs crore. docs/13 §2 finding 7: marketcap is an integer in this unit.
_CRORE: Final = Decimal("10000000")

#: SW11B (STANDING-ANSWERS A3): the two corporate-filings reads behind the catalyst feed. Both
#: are the JSON endpoints the filings pages call, per symbol, under the same cookie/header
#: discipline as the quote proxy above. The archive kinds are per symbol per day, so a morning
#: that runs twice parses the archived bytes rather than asking NSE again.
KIND_ANNOUNCEMENTS: Final = "announcements"
KIND_EVENT_CALENDAR: Final = "event-calendar"
_ANNOUNCEMENTS_API: Final = "/api/corporate-announcements?index=equities&symbol="
_EVENT_CALENDAR_API: Final = "/api/event-calendar?index=equities&symbol="
#: Where a row links out to when the calendar entry has no attachment of its own — the
#: symbol's filings page on the exchange, never a copy of anything.
_EVENT_CALENDAR_PAGE: Final = "/companies-listing/corporate-filings-event-calendar?symbol="
#: ``desc`` and NSE's one-line summary make a headline; anything longer is the filing, which
#: the feed does not carry (A3: "link out, do not reproduce text").
HEADLINE_MAX_CHARS: Final = 160
#: ``sw_catalyst.source`` values, and the record's.
CATALYST_SOURCE_ANNOUNCEMENT: Final = "NSE_ANNOUNCEMENT"
CATALYST_SOURCE_EVENT_CALENDAR: Final = "NSE_EVENT_CALENDAR"
#: An event-calendar ``purpose`` that names a result. NSE writes "Financial Results",
#: "Financial Results/Dividend", "Audited Financial Results" and the like; the word is stable.
_RESULTS_PURPOSE: Final = re.compile(r"\bresults?\b", re.IGNORECASE)

#: NSE serves its public files only to something that looks like a browser that has already
#: visited the site. These headers plus a primed cookie jar are the minimum that works.
BROWSER_HEADERS: Final[Mapping[str, str]] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Connection": "keep-alive",
}

#: index_def.slug -> the name NSE publishes the index under.
INDEX_SLUG_TO_NSE_NAME: Final[Mapping[str, str]] = {
    "nifty-50": "NIFTY 50",
    "nifty-next-50": "NIFTY NEXT 50",
    "nifty-100": "NIFTY 100",
    "nifty-200": "NIFTY 200",
    "nifty-500": "NIFTY 500",
    "nifty-total-market": "NIFTY TOTAL MARKET",
    "nifty-large-mid-250": "NIFTY LARGEMIDCAP 250",
    "nifty-midcap-150": "NIFTY MIDCAP 150",
    "nifty-smallcap-250": "NIFTY SMALLCAP 250",
    "nifty-microcap-250": "NIFTY MICROCAP 250",
    "nifty-mid-small-400": "NIFTY MIDSMALLCAP 400",
}

#: Slugs that are not published as an NSE constituent file at all. docs/06 §"Step 2" already
#: defines them by rule: `nifty-allcap` is every EQ instrument with a bar, `etf` is every ETF,
#: `nse-sme-emerge` is every instrument whose series the SME register carries.
#: docs/01 §2.1 lists `nifty-fno` as a universe but NSE publishes it as a derivatives list, not
#: an index constituent file, so it is resolved by the pipeline rather than fetched here.
DERIVED_UNIVERSES: Final[frozenset[str]] = frozenset(
    {"nifty-allcap", "etf", "nifty-fno", "nse-sme-emerge"}
)

#: The NSE Emerge (SME platform) series, as the SME register itself spells them: `SM` is the
#: normal segment, `ST` trade-for-trade, `SZ` the suspended//surveillance tail. These are real
#: listed equities on a separate NSE *platform*, not a separate exchange — they clear the same
#: CM segment, which is why one bhavcopy carries them alongside `EQ`.
SME_SERIES: Final[frozenset[str]] = frozenset({"SM", "ST", "SZ"})

#: The Emerge register. NOT the same file as `/content/equities/SME_EQUITY_L.csv`, which still
#: resolves but has been frozen at a single stale row (THEJO, 2012) for years — reading that one
#: instead is how you get an SME universe of size one. Verified live 30 Aug 2026: 565 rows.
#:
#: Its columns differ from the main register's: underscore-separated rather than space-separated,
#: and **no MARKET_LOT column** — which matters, because SME trades in lots and nothing else
#: publishes that lot size. See `DECISIONS-MERGE.md` M59 on why that keeps SME screener-only.
SME_LISTINGS_PATH: Final = "/emerge/corporates/content/SME_EQUITY_L.csv"
KIND_SME_LISTINGS: Final = "sme-listings"
#: NSE's own ETF security list — the source the `etf` universe never had (9 Sep 2026).
KIND_ETF_LIST: Final = "etf-list"
ETF_LIST_PATH: Final = "/content/equities/eq_etfseclist.csv"


class HttpResponseLike(Protocol):
    @property
    def status_code(self) -> int: ...

    @property
    def content(self) -> bytes: ...


class HttpClientLike(Protocol):
    """The slice of ``httpx.Client`` this adapter uses.

    A Protocol, not the concrete client, so the tests drive every parser and the archive-first
    ordering with zero network calls (Prompt 2 acceptance criterion 1).
    """

    def get(self, url: str, *, headers: Mapping[str, str] | None = ...) -> HttpResponseLike: ...


@dataclass(frozen=True, slots=True)
class NSERuntime:
    """The collaborators NSEProvider is wired with."""

    client: HttpClientLike | None = None
    rate_limiter: RateLimiter | None = None
    retry_policy: RetryPolicy | None = None
    retry_hooks: RetryHooks | None = None


class NSEProvider:
    """``ReferenceProvider`` backed by NSE's public files."""

    def __init__(
        self,
        settings: ProviderSettings,
        archive: RawArchive | None = None,
        runtime: NSERuntime | None = None,
    ) -> None:
        wiring = runtime or NSERuntime()
        self._settings = settings
        self._archive = archive
        self._client = wiring.client
        self._rate_limiter = wiring.rate_limiter
        self._retry_policy = wiring.retry_policy or RetryPolicy(
            max_attempts=settings.provider_max_attempts,
            base_seconds=settings.provider_backoff_base_seconds,
            max_seconds=settings.provider_backoff_max_seconds,
        )
        self._retry_hooks = wiring.retry_hooks
        self._cookies_primed = False

    # --- HealthReporting ------------------------------------------------

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    def capabilities(self) -> frozenset[Capability]:
        return REFERENCE_CAPABILITIES

    def check(self) -> ProviderHealth:
        problems: list[str] = []
        if self._archive is None:
            problems.append(
                "no raw-file archive configured; docs/09 requires archiving before parsing"
            )
        if self._client is None:
            problems.append("no HTTP client configured")
        if self._rate_limiter is None:
            problems.append(
                "no shared rate limiter configured; NSE public files are rate-sensitive"
            )

        if problems:
            return ProviderHealth(
                name=self.name,
                available=False,
                capabilities=self.capabilities(),
                detail="; ".join(problems),
            )
        archive = self._archive
        return ProviderHealth(
            name=self.name,
            available=True,
            capabilities=self.capabilities(),
            detail=f"archiving to {archive.describe() if archive else 'unknown'}",
        )

    # --- ReferenceProvider ----------------------------------------------

    def index_constituents(self, index_slug: str, on: dt.date) -> list[str]:
        """Symbols in ``index_slug`` on ``on``, from the archived constituent file."""
        if index_slug in DERIVED_UNIVERSES:
            raise UnexpectedPayload(
                f"{index_slug!r} is derived by rule, not published by NSE as a constituent file "
                "(docs/06 §'Step 2'); resolve it from instrument data instead",
                provider=self.name,
            )
        if index_slug not in UNIVERSE_BY_SLUG:
            raise UnexpectedPayload(f"unknown universe {index_slug!r}", provider=self.name)
        nse_name = INDEX_SLUG_TO_NSE_NAME.get(index_slug)
        if nse_name is None:
            raise UnexpectedPayload(
                f"no NSE constituent file is mapped for {index_slug!r}", provider=self.name
            )

        payload = self._archived(
            f"{KIND_CONSTITUENTS}/{index_slug}",
            on,
            f"{self._settings.nse_archive_url}/content/indices/{_constituent_file(index_slug)}",
        )
        frame = _read_csv(payload, context=f"{index_slug} constituents")
        context = f"{index_slug} constituents"
        column = _require_column(frame, ("Symbol", "SYMBOL", "symbol"), context)
        return [str(v) for v in frame[column].to_list() if str(v).strip()]

    def index_snapshots(self, on: dt.date) -> list[IndexSnapshot]:
        """Index level, PE, PB and dividend yield for every published index (docs/01 §7)."""
        payload = self._archived(
            KIND_INDEX_SNAPSHOT,
            on,
            f"{self._settings.nse_archive_url}/content/indices/ind_close_all_"
            f"{on.strftime('%d%m%Y')}.csv",
        )
        frame = _read_csv(payload, context="index snapshots")
        name_column = _require_column(frame, ("Index Name", "INDEX NAME"), "index snapshots")

        snapshots: list[IndexSnapshot] = []
        by_name = {name: slug for slug, name in INDEX_SLUG_TO_NSE_NAME.items()}
        for row in frame.iter_rows(named=True):
            published = str(row[name_column]).strip()
            _require_index_date(row, on, published)
            snapshots.append(
                IndexSnapshot(
                    index_slug=by_name.get(published.upper(), published),
                    date=on,
                    level=_decimal(_first(row, ("Closing Index Value", "CLOSING INDEX VALUE"))),
                    change_abs=_decimal(_first(row, ("Points Change", "POINTS CHANGE"))),
                    change_pct=_decimal(_first(row, ("Change(%)", "CHANGE(%)"))),
                    pe=_decimal(_first(row, ("P/E", "PE"))),
                    pb=_decimal(_first(row, ("P/B", "PB"))),
                    div_yield=_decimal(_first(row, ("Div Yield", "DIV YIELD"))),
                )
            )
        return snapshots

    def corporate_actions(self, since: dt.date) -> list[CorporateAction]:
        """Splits, bonuses, dividends and rights with an ex-date on or after ``since``.

        **The window is not optional.** Asked without ``from_date``/``to_date``, NSE answers with
        a default first page of 20 rows and no indication that it has done so; the nightly stored
        those 20 as though they were the day's corporate actions, for every night the feed ran.
        NSE published 87 actions for 2026-09-11 alone. `gates/ca-truncation.md` has the evidence.

        The window runs forward from ``since`` rather than stopping at today, because NSE
        announces an ex-date ahead of time and an action is worth *storing* before it is worth
        *applying*. Nothing about that is a look-ahead: `apply_adjustments` bounds itself to
        ``ex_date <= as_of`` (docs/DECISIONS.md §21.9), so a future-dated row sits inert in
        `corporate_action` until its ex-date arrives. Deriving the far edge from ``since`` and not
        from the clock also keeps the request a pure function of the archive key, so one archived
        payload always answers exactly one question.
        """
        until = since + dt.timedelta(days=self._settings.nse_corporate_action_window_days)
        payload = self._archived(
            KIND_CORPORATE_ACTIONS_WINDOWED,
            since,
            f"{self._settings.nse_base_url}/api/corporates-corporateActions?index=equities"
            f"&from_date={_nse_day(since)}&to_date={_nse_day(until)}",
            extension="json",
            content_type="application/json",
        )
        frame = _read_json(payload, context="corporate actions")
        self._refuse_default_page(frame.height, since, until)
        actions: list[CorporateAction] = []
        for row in frame.iter_rows(named=True):
            symbol = str(_first(row, ("symbol", "SYMBOL")) or "").strip()
            purpose = str(_first(row, ("subject", "purpose", "SUBJECT")) or "")
            ex_date = _date(_first(row, ("exDate", "EX_DATE", "ex_date")))
            if not symbol or ex_date is None or ex_date < since:
                continue
            parsed_legs = parse_corporate_action_purposes(purpose)
            if not parsed_legs:
                continue
            for action_type, ratio_from, ratio_to, amount in parsed_legs:
                actions.append(
                    CorporateAction(
                        symbol=symbol,
                        action_type=action_type,
                        ex_date=ex_date,
                        ratio_from=ratio_from,
                        ratio_to=ratio_to,
                        amount=amount,
                        raw=dict(row),
                    )
                )
        return actions

    def listings(self) -> list[ListingRecord]:
        """The NSE main-board listings register (docs/01 §1: 3,524 rows on /listings).

        Main board only. The Emerge (SME) platform has a register of its own — a name appears in
        exactly one of the two — and :meth:`sme_listings` reads it.
        """
        return self._listings_from(
            KIND_LISTINGS, "/content/equities/EQUITY_L.csv", context="listings"
        )

    def sme_listings(self) -> list[ListingRecord]:
        """The NSE Emerge (SME) register — the other half of the listing universe.

        Separate from :meth:`listings` rather than merged into it so that an Emerge outage is a
        fact the pipeline step can record and carry, instead of an exception this layer would
        have to swallow to keep 2,559 main-board rows refreshing. Same reason the two are
        separate archive kinds: one can be re-read without re-reading the other.
        """
        return self._listings_from(KIND_SME_LISTINGS, SME_LISTINGS_PATH, context="sme listings")

    def etf_symbols(self) -> list[str]:
        """Every ETF listed on NSE — the ``etf`` universe's membership (9 Sep 2026).

        WHY THIS EXISTS. `etf` screened to nothing on every run.
        `membership.DERIVED_BY_RULE` resolves it as ``instrument.instrument_type == "ETF"`` and
        **nothing ever writes that type** — the instrument table holds only EQ and INDEX — so the
        rule selected an empty set and the screen honestly returned nothing for an empty universe.
        `quality.NO_MEMBERSHIP_SOURCE` recorded the gap rather than fixing it.

        NSE publishes the list: ``eq_etfseclist.csv``, 350 rows, a ``Symbol`` column.

        **Why this and not reclassifying instruments.** The obvious fix is to stamp
        ``instrument_type = "ETF"`` during the listings ingest, which is what the note in
        `quality.py` proposed. It would also silently REMOVE those 350 names from every
        EQ-derived universe — `nifty-allcap` is "every EQ instrument", and it currently carries
        4,275 including the ETFs. Changing what a name *is* to fix which list it appears on is a
        much larger change than the one being asked for, and it would shrink a universe nobody
        asked to shrink. Sourcing the membership leaves every other universe exactly as it is.
        """
        payload = self._archived(
            KIND_ETF_LIST, dt.date.today(), f"{self._settings.nse_archive_url}{ETF_LIST_PATH}"
        )
        frame = _read_csv(payload, context="etf list")
        for column in ("Symbol", "SYMBOL", "symbol"):
            if column in frame.columns:
                return sorted(
                    {
                        str(value).strip().upper()
                        for value in frame[column].to_list()
                        if value is not None and str(value).strip()
                    }
                )
        raise UnexpectedPayload(f"the ETF list has no Symbol column: {frame.columns}")

    def _listings_from(self, kind: str, path: str, *, context: str) -> list[ListingRecord]:
        """One listings register. The two files spell their headers differently, so every
        lookup below names both spellings."""
        payload = self._archived(kind, dt.date.today(), f"{self._settings.nse_archive_url}{path}")
        frame = _read_csv(payload, context=context)
        records: list[ListingRecord] = []
        for row in frame.iter_rows(named=True):
            symbol = str(_first(row, ("SYMBOL", "Symbol")) or "").strip()
            if not symbol:
                continue
            records.append(
                ListingRecord(
                    symbol=symbol,
                    name=str(_first(row, ("NAME OF COMPANY", "NAME_OF_COMPANY")) or symbol).strip(),
                    series=_clean(_first(row, (" SERIES", "SERIES"))),
                    isin=_clean(_first(row, (" ISIN NUMBER", "ISIN NUMBER", "ISIN_NUMBER"))),
                    listed_on=_date(
                        _first(row, (" DATE OF LISTING", "DATE OF LISTING", "DATE_OF_LISTING"))
                    ),
                    face_value=_decimal(_first(row, (" FACE VALUE", "FACE VALUE", "FACE_VALUE"))),
                    paid_up_value=_decimal(
                        _first(row, (" PAID UP VALUE", "PAID UP VALUE", "PAID_UP_VALUE"))
                    ),
                    # Absent from the Emerge register entirely; None, never a guessed 1.
                    market_lot=_int(_first(row, (" MARKET LOT", "MARKET LOT", "MARKET_LOT"))),
                )
            )
        return records

    def bhavcopy(self, on: dt.date) -> pl.DataFrame:
        """One trading day's bhavcopy, including series and circuit bands (docs/09)."""
        payload = self._archived(
            KIND_BHAVCOPY,
            on,
            f"{self._settings.nse_archive_url}/content/cm/BhavCopy_NSE_CM_0_0_0_"
            f"{on.strftime('%Y%m%d')}_F_0000.csv.zip",
            extension="zip",
            content_type="application/zip",
        )
        frame = _read_csv(_maybe_unzip(payload), context=f"bhavcopy {on.isoformat()}")
        return _bhavcopy_to_frame(frame, on)

    def equity_fundamentals(
        self,
        on: dt.date,
        symbols: Sequence[str],
        *,
        series_by_symbol: Mapping[str, str] | None = None,
    ) -> list[EquityFundamental]:
        """Issued size * last price and the published P/E, archived per symbol.

        Folded into the nightly snapshots step rather than given a twelfth pipeline identity
        (docs/03's ten plus M30's cache). A name NSE does not quote is skipped; the join stores
        NULL and the UI renders an em dash — the same honesty as an index with no PE.

        ``series_by_symbol`` is an optional hint (the caller usually has ``instrument.series``
        already). It is only a hint: a symbol whose hinted series returns an empty quote falls
        back to :meth:`_resolve_series`, so a stale hint costs a round trip, never a NULL row.
        """
        records: list[EquityFundamental] = []
        hints = {k.upper(): v for k, v in (series_by_symbol or {}).items()}
        for symbol in symbols:
            token = symbol.strip().upper()
            if not token:
                continue
            parsed = self._equity_fundamental(on, token, hints.get(token))
            if parsed is not None:
                records.append(parsed)
        return records

    def _equity_fundamental(
        self, on: dt.date, token: str, series_hint: str | None
    ) -> EquityFundamental | None:
        """One symbol's quote: try the hinted series, then the series NSE itself reports.

        NSE answers a wrong series with ``200`` and an empty ``equityResponse`` rather than an
        error, so "no rows" is indistinguishable from "wrong series" at the HTTP layer. That is
        why the empty case escalates to ``getMetaData`` instead of being recorded as a miss.
        """
        tried: set[str] = set()
        candidates = [series_hint] if series_hint else []
        for series in candidates:
            record = self._quote(on, token, series, tried)
            if record is not None:
                return record
        # The hint was absent or wrong. Ask NSE which series it quotes this name under; a series
        # already tried is not retried, so a correct hint costs exactly one request.
        resolved = self._resolve_series(on, token)
        if resolved is not None:
            return self._quote(on, token, resolved, tried)
        return None

    def _quote(
        self, on: dt.date, token: str, series: str, tried: set[str]
    ) -> EquityFundamental | None:
        if series in tried:
            return None
        tried.add(series)
        payload = self._quote_payload(on, token, series)
        if payload is None:
            return None
        return parse_equity_quote(payload, on=on, fallback_symbol=token)

    def _quote_payload(self, on: dt.date, token: str, series: str) -> bytes | None:
        try:
            return self._archived(
                f"{KIND_EQUITY_FUNDAMENTALS}/{token}-{series}",
                on,
                f"{self._settings.nse_base_url}{_GET_QUOTE_API}"
                f"?functionName=getSymbolData&marketType=N"
                f"&series={quote(series)}&symbol={quote(token)}",
                extension="json",
                content_type="application/json",
            )
        except UnexpectedPayload:
            # 404 — NSE does not list this name. Recorded by absence, not by an invented row.
            return None

    def _resolve_series(self, on: dt.date, token: str) -> str | None:
        """Ask NSE which series it actually quotes this symbol under today."""
        try:
            payload = self._archived(
                f"{KIND_EQUITY_META}/{token}",
                on,
                f"{self._settings.nse_base_url}{_GET_QUOTE_API}"
                f"?functionName=getMetaData&symbol={quote(token)}",
                extension="json",
                content_type="application/json",
            )
        except UnexpectedPayload:
            return None
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        active = data.get("activeSeries")
        if not isinstance(active, list):
            return None
        codes = [str(code).strip().upper() for code in active if str(code).strip()]
        if not codes:
            return None
        return "EQ" if "EQ" in codes else codes[0]

    # --- SW11B: the catalyst feed (STANDING-ANSWERS A3) -------------------

    def announcements(self, symbols: Sequence[str], *, on: dt.date) -> list[CatalystRecord]:
        """Corporate announcements for ``symbols``, newest first per symbol.

        One archived request per symbol, each through the cookie prime and the limiter. A
        symbol NSE answers with 404 (renamed, delisted, never listed) yields no rows — recorded
        by absence, exactly as :meth:`equity_fundamentals` treats a missing quote. Anything
        else that goes wrong — a 429, a 5xx, an unparseable body — raises the provider's own
        error type; the caller decides how soft to fail, not this layer.
        """
        records: list[CatalystRecord] = []
        for symbol in symbols:
            token = symbol.strip().upper()
            if not token:
                continue
            payload = self._filings_payload(KIND_ANNOUNCEMENTS, _ANNOUNCEMENTS_API, token, on)
            if payload is not None:
                records.extend(
                    parse_announcements(
                        payload,
                        fallback_symbol=token,
                        archive_url=self._settings.nse_archive_url,
                    )
                )
        return records

    def results_calendar(self, symbols: Sequence[str], *, on: dt.date) -> list[EarningsDateRecord]:
        """Board meetings whose purpose is a financial result, from NSE's event calendar.

        Every listed meeting is returned, past and future; the caller picks the one it wants
        (the nearest on or after the session, for the watchlist's flag). Same absence rule
        and the same error discipline as :meth:`announcements`.
        """
        records: list[EarningsDateRecord] = []
        page = f"{self._settings.nse_base_url}{_EVENT_CALENDAR_PAGE}"
        for symbol in symbols:
            token = symbol.strip().upper()
            if not token:
                continue
            payload = self._filings_payload(KIND_EVENT_CALENDAR, _EVENT_CALENDAR_API, token, on)
            if payload is not None:
                records.extend(
                    parse_results_calendar(payload, fallback_symbol=token, page_url=page)
                )
        return records

    def _filings_payload(self, kind: str, api: str, token: str, on: dt.date) -> bytes | None:
        try:
            return self._archived(
                f"{kind}/{token}",
                on,
                f"{self._settings.nse_base_url}{api}{quote(token)}",
                extension="json",
                content_type="application/json",
            )
        except UnexpectedPayload as exc:
            # 404 — NSE does not know this name today. Absence, not an invented row. Every
            # other 4xx is a real refusal and stays loud.
            if "returned 404" in str(exc):
                return None
            raise

    # --- internals ------------------------------------------------------

    def _refuse_default_page(self, rows: int, since: dt.date, until: dt.date) -> None:
        """Refuse a payload shaped like NSE's default page rather than an answer to our window.

        Defence in depth behind the window itself: the range is the fix, this is what makes a
        silent regression of it impossible. Only a window long enough to make 20 rows absurd is
        judged, so an honestly quiet week is never called truncated — over 120 days NSE returns
        hundreds of rows, and the twelve archived payloads that exposed this were 20 every time.

        Refusing is the cheap side of the trade. A false alarm costs one loud night that an
        operator resolves by reading the archived file; accepting a default page costs an adjusted
        price series that is quietly wrong for every instrument whose action fell outside it, and
        a momentum factor computed on top of that.
        """
        if rows != NSE_DEFAULT_PAGE_ROWS:
            return
        if (until - since).days <= NSE_DEFAULT_PAGE_ROWS:
            return
        raise UnexpectedPayload(
            f"corporate actions for {since.isoformat()}..{until.isoformat()} came back as exactly "
            f"{NSE_DEFAULT_PAGE_ROWS} rows, which is the page NSE serves when it ignores the "
            f"date range. Refusing to treat it as the window's full contents — see "
            f"gates/ca-truncation.md.",
            provider=PROVIDER_NAME,
        )

    def _archived(
        self,
        kind: str,
        on: dt.date,
        url: str,
        *,
        extension: str = "csv",
        content_type: str = "text/csv",
    ) -> bytes:
        """Archive-then-parse (docs/09). Returns the bytes read *back* from the archive."""
        if self._archive is None:
            raise ProviderUnavailable(
                "no raw-file archive configured. docs/09 requires every NSE file to be archived "
                "before it is parsed, so parsing is reproducible without refetching.",
                provider=self.name,
            )
        key = archive_key(kind, on, extension)
        return fetch_and_archive(
            self._archive,
            key,
            lambda: self._fetch(url),
            content_type=content_type,
        )

    def _fetch(self, url: str) -> bytes:
        """Prime cookies, throttle, then fetch — under the retry policy."""

        def attempt() -> bytes:
            return self._request(url)

        return call_with_retry(
            attempt, self._retry_policy, provider=self.name, hooks=self._retry_hooks
        )

    def _require_client(self) -> HttpClientLike:
        if self._client is None:
            raise ProviderUnavailable("no HTTP client configured", provider=self.name)
        return self._client

    def _prime_cookies(self, client: HttpClientLike) -> None:
        """docs/09: NSE's public files need a browser-like session.

        One visit to the site root before the first archive request; the client's cookie jar
        carries the result. Primed once per provider instance, not per request, because
        re-priming on every file is itself the kind of traffic pattern that gets throttled.
        AF 3.11: a 401/403 clears the flag so the next call re-primes rather than looping on a
        stale jar.
        """
        if self._cookies_primed:
            return
        self._throttle()
        _get(client, self._settings.nse_base_url)
        self._cookies_primed = True

    def _throttle(self) -> None:
        """AF 3.11: a missing limiter is a hard refuse, not a silent no-op.

        An unthrottled NSE client is how the §17 ~600-request stall happens. ``check()`` already
        reports the gap; every live request must enforce it.
        """
        if self._rate_limiter is None:
            raise ProviderUnavailable(
                "NSE rate limiter is required; refusing an unthrottled request",
                provider=self.name,
            )
        self._rate_limiter.acquire()

    def _request(self, url: str) -> bytes:
        """Throttled GET with a one-shot cookie re-prime on 401/403 (AF 3.11)."""
        client = self._require_client()
        self._prime_cookies(client)
        self._throttle()
        response = _get(client, url)
        if response.status_code in (401, 403):
            self._cookies_primed = False
            self._prime_cookies(client)
            self._throttle()
            response = _get(client, url)
        return _body(response, url)


def build_http_client(settings: ProviderSettings) -> HttpClientLike:
    """A browser-shaped httpx client with a cookie jar that persists across requests."""
    client: HttpClientLike = httpx.Client(
        headers=dict(BROWSER_HEADERS),
        timeout=settings.nse_request_timeout_seconds,
        follow_redirects=True,
    )
    return client


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

_HTTP_TOO_MANY_REQUESTS: Final = 429
_HTTP_SERVER_ERROR: Final = 500


def _get(client: HttpClientLike, url: str) -> HttpResponseLike:
    try:
        return client.get(url)
    except Exception as exc:
        raise UpstreamUnavailable(f"GET {url} failed: {exc}", provider=PROVIDER_NAME) from exc


def _body(response: HttpResponseLike, url: str) -> bytes:
    status = response.status_code
    if status == _HTTP_TOO_MANY_REQUESTS:
        raise UpstreamUnavailable(f"NSE rate-limited GET {url}", provider=PROVIDER_NAME)
    if status >= _HTTP_SERVER_ERROR:
        raise UpstreamUnavailable(f"GET {url} returned {status}", provider=PROVIDER_NAME)
    if status >= 400:  # noqa: PLR2004 - the standard client-error boundary
        raise UnexpectedPayload(f"GET {url} returned {status}", provider=PROVIDER_NAME)
    return response.content


def _maybe_unzip(payload: bytes) -> bytes:
    """NSE ships the bhavcopy as a single-entry zip; older files are plain CSV."""
    if not payload.startswith(b"PK"):
        return payload
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as bundle:
            names = bundle.namelist()
            if not names:
                raise UnexpectedPayload("archived zip is empty", provider=PROVIDER_NAME)
            return bundle.read(names[0])
    except zipfile.BadZipFile as exc:
        raise UnexpectedPayload(
            f"archived file is not a readable zip: {exc}", provider=PROVIDER_NAME
        ) from exc


def _require_index_date(row: Mapping[str, object], on: dt.date, published: str) -> None:
    """SW16: the file for ``on`` must say ``on`` in its own ``Index Date`` column.

    ``ind_close_all_DDMMYYYY.csv`` is fetched by the date in its name, and the archive is keyed by
    the date it was asked for. A file that carries another session's date under this key is the
    wrong file — an edge cache, a redirect to the latest, a mistyped key in a repair — and every
    level in it would land on the wrong day and look plausible. That is structural damage, which
    docs/09 gives the parser, not the QA gate, the job of refusing. A file with no date column
    (older layouts) is left to the columns that are there.
    """
    raw = _first(row, ("Index Date", "INDEX DATE"))
    if raw is None:
        return
    stamped = _date(raw)
    if stamped is not None and stamped != on:
        raise UnexpectedPayload(
            f"index snapshots for {on.isoformat()}: row {published!r} is dated "
            f"{stamped.isoformat()}; wrong file for this date",
            provider=PROVIDER_NAME,
        )


def _read_csv(payload: bytes, *, context: str) -> pl.DataFrame:
    try:
        return pl.read_csv(io.BytesIO(payload), infer_schema_length=None, try_parse_dates=False)
    except Exception as exc:
        raise UnexpectedPayload(
            f"could not parse {context} as CSV: {exc}", provider=PROVIDER_NAME
        ) from exc


def _read_json(payload: bytes, *, context: str) -> pl.DataFrame:
    try:
        return pl.read_json(io.BytesIO(payload))
    except Exception as exc:
        raise UnexpectedPayload(
            f"could not parse {context} as JSON: {exc}", provider=PROVIDER_NAME
        ) from exc


def _require_column(frame: pl.DataFrame, candidates: tuple[str, ...], context: str) -> str:
    for candidate in candidates:
        if candidate in frame.columns:
            return candidate
    raise UnexpectedPayload(
        f"{context}: none of the expected columns {list(candidates)} are present; "
        f"got {frame.columns}",
        provider=PROVIDER_NAME,
    )


def _nse_day(on: dt.date) -> str:
    """``DD-MM-YYYY`` — the only date spelling ``corporates-corporateActions`` accepts.

    Give it an ISO date and it does not complain; it silently ignores the range and serves the
    default page, which is the failure this whole module comment exists about.
    """
    return on.strftime("%d-%m-%Y")


def _first(row: Mapping[str, object], candidates: tuple[str, ...]) -> object:
    for candidate in candidates:
        if candidate in row:
            return row[candidate]
    return None


def _clean(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _decimal(value: object) -> Decimal | None:
    text = _clean(value)
    if text is None or text in {"-", "NA", "N/A", "nan"}:
        return None
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


def _int(value: object) -> int | None:
    text = _clean(value)
    if text is None:
        return None
    try:
        return int(float(text.replace(",", "")))
    except ValueError:
        return None


_DATE_FORMATS: Final[tuple[str, ...]] = ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d-%b-%y")


def _date(value: object) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = _clean(value)
    if text is None:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt).replace(tzinfo=dt.UTC).date()
        except ValueError:
            continue
    return None


#: Constituent files whose name is NOT ``ind_{slug-without-hyphens}list.csv``.
#:
#: Verified against the live archive on 2026-08-22 (M8). NSE is inconsistent in two ways and
#: neither is derivable from the slug: some names take an underscore before ``list`` and some do
#: not, and two indices use a longer word than the slug does ("largemidcap" for
#: ``nifty-large-mid-250``, "midsmallcap" for ``nifty-mid-small-400``). Every entry below was a
#: 404 under the old rule, and every one was confirmed 200 with the name given here.
_CONSTITUENT_FILE_OVERRIDES: Final[Mapping[str, str]] = {
    "nifty-total-market": "ind_niftytotalmarket_list.csv",
    "nifty-large-mid-250": "ind_niftylargemidcap250list.csv",
    "nifty-microcap-250": "ind_niftymicrocap250_list.csv",
    "nifty-mid-small-400": "ind_niftymidsmallcap400list.csv",
}


def _constituent_file(index_slug: str) -> str:
    """The constituent CSV's filename for an index slug.

    The rule covers seven of the eleven published universes; the rest are in
    :data:`_CONSTITUENT_FILE_OVERRIDES`, named rather than guessed. Adding a universe means
    checking the real archive, not extending the rule and hoping.
    """
    override = _CONSTITUENT_FILE_OVERRIDES.get(index_slug)
    if override is not None:
        return override
    return f"ind_{_file_token(index_slug)}list.csv"


def _file_token(index_slug: str) -> str:
    """NSE names most of its constituent files ``ind_nifty50list.csv`` and similar."""
    return index_slug.replace("-", "")


def _bhavcopy_to_frame(frame: pl.DataFrame, on: dt.date) -> pl.DataFrame:
    """Map NSE's bhavcopy columns onto ``BHAVCOPY_SCHEMA``.

    NSE has published this file under two different column vocabularies; both are accepted so a
    backfill spanning the change does not need two code paths.
    """
    aliases: Mapping[str, tuple[str, ...]] = {
        "symbol": ("TckrSymb", "SYMBOL"),
        "series": ("SctySrs", "SERIES"),
        "open": ("OpnPric", "OPEN"),
        "high": ("HghPric", "HIGH"),
        "low": ("LwPric", "LOW"),
        "close": ("ClsPric", "CLOSE"),
        "prev_close": ("PrvsClsgPric", "PREVCLOSE"),
        "volume": ("TtlTradgVol", "TOTTRDQTY"),
        "turnover": ("TtlTrfVal", "TOTTRDVAL"),
        "trades": ("TtlNbOfTxsExctd", "TOTALTRADES"),
        "upper_circuit": ("UpprBookgPric", "UPPER_BAND"),
        "lower_circuit": ("LwrBookgPric", "LOWER_BAND"),
    }

    rows: list[dict[str, object]] = []
    for row in frame.iter_rows(named=True):
        symbol = _clean(_first(row, aliases["symbol"]))
        if symbol is None:
            continue
        rows.append(
            {
                "symbol": symbol,
                "series": _clean(_first(row, aliases["series"])),
                "date": on,
                "open": _decimal(_first(row, aliases["open"])),
                "high": _decimal(_first(row, aliases["high"])),
                "low": _decimal(_first(row, aliases["low"])),
                "close": _decimal(_first(row, aliases["close"])),
                "prev_close": _decimal(_first(row, aliases["prev_close"])),
                "volume": _int(_first(row, aliases["volume"])),
                "turnover": _decimal(_first(row, aliases["turnover"])),
                "trades": _int(_first(row, aliases["trades"])),
                "upper_circuit": _decimal(_first(row, aliases["upper_circuit"])),
                "lower_circuit": _decimal(_first(row, aliases["lower_circuit"])),
            }
        )
    if not rows:
        raise UnexpectedPayload(
            f"bhavcopy for {on.isoformat()} contained no readable rows", provider=PROVIDER_NAME
        )
    return conform(pl.DataFrame(rows, strict=False), BHAVCOPY_SCHEMA)


ParsedAction = tuple[str, Decimal | None, Decimal | None, Decimal | None]


def _parse_split(text: str) -> ParsedAction | None:
    """``FACE VALUE SPLIT FROM RS.10/- TO RE.1/-`` or a bare ``10:1``."""
    faces = re.search(r"(?:RS\.?|RE\.?)\s*([\d.]+)\D+(?:RS\.?|RE\.?)\s*([\d.]+)", text)
    if faces:
        return ("split", _to_decimal(faces.group(1)), _to_decimal(faces.group(2)), None)
    pair = _ratio_pair(text)
    return ("split", pair[0], pair[1], None) if pair else None


def _parse_bonus(text: str) -> ParsedAction | None:
    pair = _ratio_pair(text)
    return ("bonus", pair[0], pair[1], None) if pair else None


def _parse_rights(text: str) -> ParsedAction:
    pair = _ratio_pair(text)
    return ("rights", pair[0], pair[1], None) if pair else ("rights", None, None, None)


def _parse_demerger(text: str) -> ParsedAction:
    del text
    return ("demerger", None, None, None)


def _parse_dividend(text: str) -> ParsedAction:
    """Sum every ``RS x`` / ``RE x`` amount in the purpose (AF 3.3).

    NSE writes combined cash legs as ``DIVIDEND - RS.2.50 + RS.1.00 PER SHARE``; keeping only
    the first amount understated the cash adjustment.
    """
    amounts = [
        value
        for match in re.findall(r"(?:RS\.?|RE\.?)\s*([\d.]+)", text)
        if (value := _to_decimal(match)) is not None
    ]
    if not amounts:
        return ("dividend", None, None, None)
    total = amounts[0]
    for extra in amounts[1:]:
        total += extra
    return ("dividend", None, None, total)


#: Keyword -> parser. Order matters only for single-purpose strings; multi-leg purposes are
#: split on AND before each segment is matched (AF 3.2).
_PURPOSE_PARSERS: Final[tuple[tuple[str, Callable[[str], ParsedAction | None]], ...]] = (
    ("SPLIT", _parse_split),
    ("BONUS", _parse_bonus),
    ("RIGHTS", _parse_rights),
    ("DEMERGER", _parse_demerger),
    ("DIVIDEND", _parse_dividend),
)


def _ratio_pair(text: str) -> tuple[Decimal | None, Decimal | None] | None:
    match = re.search(r"(\d+)\s*:\s*(\d+)", text)
    if not match:
        return None
    return _to_decimal(match.group(1)), _to_decimal(match.group(2))


def parse_equity_quote(
    payload: bytes, *, on: dt.date, fallback_symbol: str
) -> EquityFundamental | None:
    """Read whichever equity-quote shape NSE served into one typed record.

    Two shapes exist on disk. The archive is permanent (docs/09), so payloads captured before
    NSE's Next.js migration must keep parsing forever; the live site serves the ``GetQuoteApi``
    shape. Dispatching on the payload rather than on a config flag means a re-parse of the
    archive never has to know when the file was fetched.
    """
    data = _quote_json(payload, fallback_symbol)
    if "equityResponse" in data:
        return parse_get_symbol_data(payload, on=on, fallback_symbol=fallback_symbol)
    return parse_quote_equity(payload, on=on, fallback_symbol=fallback_symbol)


def parse_get_symbol_data(
    payload: bytes, *, on: dt.date, fallback_symbol: str
) -> EquityFundamental | None:
    """Read ``GetQuoteApi?functionName=getSymbolData`` into a typed record.

    The live payload nests one entry per series under ``equityResponse``, each carrying
    ``tradeInfo`` (issued size, last price, NSE's own total market cap), ``secInfo`` (the
    published symbol P/E) and ``metaData``. A wrong series is answered with ``200`` and an
    **empty** list, so an empty response returns ``None`` rather than an empty-but-present row.

    ``pb`` and ``div_yield`` are not in this payload at all. They stay NULL: an em dash is the
    truth, and carrying the old shape's field names forward would only make an absent number
    look like a fetched one.
    """
    data = _quote_json(payload, fallback_symbol)
    entries = data.get("equityResponse")
    if not isinstance(entries, list) or not entries:
        return None
    entry = entries[0]
    if not isinstance(entry, dict):
        return None

    trade = _section(entry, "tradeInfo")
    security = _section(entry, "secInfo")
    meta = _section(entry, "metaData")
    order = _section(entry, "orderBook")

    symbol = _clean(_first(meta, ("symbol",))) or fallback_symbol
    shares = _int(_first(trade, ("issuedSize",)))
    last = (
        _decimal(_first(trade, ("lastPrice",)))
        or _decimal(_first(meta, ("closePrice", "lastPrice")))
        or _decimal(_first(order, ("lastPrice",)))
    )
    pe = _decimal(_first(security, ("pdSymbolPe",)))

    marketcap_cr = _marketcap_from_shares(shares, last)
    if marketcap_cr is None:
        # NSE's own figure, already in rupees, when we cannot derive it ourselves.
        total = _decimal(_first(trade, ("totalMarketCap",)))
        if total is not None and total > 0:
            marketcap_cr = int((total / _CRORE).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    if marketcap_cr is None and pe is None and shares is None:
        return None
    return EquityFundamental(
        symbol=symbol,
        date=on,
        shares_outstanding=shares,
        last_price=last,
        marketcap_cr=marketcap_cr,
        pe=pe,
        pb=None,
        div_yield=None,
    )


def _quote_json(payload: bytes, fallback_symbol: str) -> dict[str, object]:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise UnexpectedPayload(
            f"equity quote for {fallback_symbol} is not JSON: {exc}", provider=PROVIDER_NAME
        ) from exc
    if not isinstance(data, dict):
        raise UnexpectedPayload(
            f"equity quote for {fallback_symbol} is not an object", provider=PROVIDER_NAME
        )
    return data


def _section(data: Mapping[str, object], key: str) -> Mapping[str, object]:
    """One nested object, or an empty mapping. NSE omits whole sections for some names."""
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def _marketcap_from_shares(shares: int | None, price: Decimal | None) -> int | None:
    if shares is None or price is None or price <= 0:
        return None
    return int((Decimal(shares) * price / _CRORE).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def parse_quote_equity(
    payload: bytes, *, on: dt.date, fallback_symbol: str
) -> EquityFundamental | None:
    """Read NSE's retired ``quote-equity`` JSON into a typed record.

    Kept for the archive, not for the network: NSE no longer serves this route. Payloads
    captured before the migration still parse, which is the whole point of archiving raw bytes.

    That payload nests ``info``, ``securityInfo``, ``priceInfo`` and ``metadata``. A missing
    issued size or last price leaves ``marketcap_cr`` NULL rather than inventing a zero cap.
    """
    data = _quote_json(payload, fallback_symbol)

    info = _section(data, "info")
    security = _section(data, "securityInfo")
    price = _section(data, "priceInfo")
    meta = _section(data, "metadata")
    symbol = _clean(_first(info, ("symbol",))) or fallback_symbol
    shares = _int(_first(security, ("issuedSize", "issued_size")))
    last = _decimal(_first(price, ("lastPrice", "close", "last_price")))
    pe = _decimal(_first(price, ("pE", "pe"))) or _decimal(_first(meta, ("pdSymbolPe",)))
    pb = _decimal(_first(price, ("pB", "pb"))) or _decimal(
        _first(meta, ("pdSymbolPb", "pdSectorPb"))
    )
    div_yield = _decimal(_first(price, ("yield", "divYield")))
    marketcap_cr = _marketcap_from_shares(shares, last)
    if marketcap_cr is None and pe is None and shares is None:
        return None
    return EquityFundamental(
        symbol=symbol,
        date=on,
        shares_outstanding=shares,
        last_price=last,
        marketcap_cr=marketcap_cr,
        pe=pe,
        pb=pb,
        div_yield=div_yield,
    )


def parse_corporate_action_purpose(purpose: str) -> ParsedAction | None:
    """Read NSE's free-text "purpose" field into a typed action.

    Prefer :func:`parse_corporate_action_purposes` when a row may carry multiple legs — this
    helper returns the first leg only, for callers that still expect a single tuple.
    """
    legs = parse_corporate_action_purposes(purpose)
    return legs[0] if legs else None


def parse_corporate_action_purposes(purpose: str) -> list[ParsedAction]:
    """Every modelled leg in an NSE purpose string (AF 3.2).

    NSE publishes combined purposes such as ``FACE VALUE SPLIT … AND BONUS 1:1``. Matching the
    first keyword alone dropped the bonus and understated the factor by 2x. Segments are split
    on ``AND`` / ``&``; each segment contributes at most one action. Unmodelled text yields an
    empty list rather than a guess.
    """
    text = purpose.upper().strip()
    if not text:
        return []
    segments = [part.strip() for part in re.split(r"\s+(?:AND|&)\s+", text) if part.strip()]
    if len(segments) == 1:
        segments = [text]
    found: list[ParsedAction] = []
    for segment in segments:
        for keyword, parse in _PURPOSE_PARSERS:
            if keyword not in segment:
                continue
            parsed = parse(segment)
            if parsed is not None:
                found.append(parsed)
            break
    return found


def _to_decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


# ---------------------------------------------------------------------------
# SW11B: the catalyst feed's two parsers (STANDING-ANSWERS A3)
# ---------------------------------------------------------------------------

#: The exchange stamps announcements in IST, without an offset. ``published_at`` is stored as
#: timestamptz, so the offset is attached here, once, where the source's convention is known.
IST: Final = dt.timezone(dt.timedelta(hours=5, minutes=30), name="Asia/Kolkata")

_STAMP_FORMATS: Final[tuple[str, ...]] = (
    "%d-%b-%Y %H:%M:%S",  # an_dt / dt: "01-Sep-2026 18:32:11"
    "%Y-%m-%d %H:%M:%S",  # sort_date: "2026-09-01 18:32:11"
    "%d-%b-%Y",  # a date-only stamp, seen on older rows
)


def _stamp(value: object) -> dt.datetime | None:
    text = _clean(value)
    if text is None:
        return None
    for fmt in _STAMP_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def _first_stamp(row: Mapping[str, object], candidates: tuple[str, ...]) -> dt.datetime | None:
    """The first candidate that *parses* — NSE ships ``an_dt`` as ``""`` beside a filled
    ``sort_date`` often enough that "first key present" would lose the stamp."""
    for candidate in candidates:
        stamp = _stamp(row.get(candidate))
        if stamp is not None:
            return stamp
    return None


def _filings_rows(payload: bytes, *, context: str) -> list[dict[str, object]]:
    """The list NSE's filings endpoints answer with. A ``{}`` or a non-list is damage; an
    empty list is an honest "nothing filed" and parses to no rows."""
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise UnexpectedPayload(
            f"could not parse {context} as JSON: {exc}", provider=PROVIDER_NAME
        ) from exc
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        data = data["data"]
    if not isinstance(data, list):
        raise UnexpectedPayload(
            f"{context}: expected a JSON list, got {type(data).__name__}", provider=PROVIDER_NAME
        )
    return [row for row in data if isinstance(row, dict)]


def _headline(subject: str | None, summary: str | None) -> str | None:
    """NSE's subject line plus its one-line summary, capped. Never the attachment body."""
    parts: list[str] = []
    if summary and subject and summary.lower().startswith(subject.lower()):
        parts.append(summary)  # NSE repeats the subject at the head of its summary
    else:
        parts.extend(part for part in (subject, summary) if part)
    text = " — ".join(" ".join(part.split()) for part in parts)
    if not text:
        return None
    if len(text) > HEADLINE_MAX_CHARS:
        text = text[: HEADLINE_MAX_CHARS - 1].rstrip() + "…"
    return text


def _absolute(url: str, base: str) -> str:
    """NSE's attachment links are absolute; a relative one is joined, never guessed at."""
    if url.startswith(("http://", "https://")):
        return url
    return f"{base}/{url.lstrip('/')}"


def parse_announcements(
    payload: bytes, *, fallback_symbol: str, archive_url: str
) -> list[CatalystRecord]:
    """Read ``/api/corporate-announcements`` into records — headline, stamp and link only.

    A row without an attachment URL is dropped and not invented: the feed is links, and a
    headline with nowhere to link is text the feed would be reproducing. A row without a stamp
    is kept with ``published_at=None`` — it is still a link, it just cannot be "newest".
    Newest first, undated rows last, so a caller taking the first row takes the latest.
    """
    rows = _filings_rows(payload, context=f"{fallback_symbol} announcements")
    records: list[CatalystRecord] = []
    for row in rows:
        url = _clean(_first(row, ("attchmntFile", "attachmentFile", "attchmntfile")))
        if url is None:
            continue
        headline = _headline(
            _clean(_first(row, ("desc", "subject", "sm_desc"))),
            _clean(_first(row, ("attchmntText", "attachmentText"))),
        )
        if headline is None:
            continue
        records.append(
            CatalystRecord(
                symbol=_clean(_first(row, ("symbol", "SYMBOL"))) or fallback_symbol,
                headline=headline,
                published_at=_first_stamp(row, ("an_dt", "sort_date", "dt", "exchdisstime")),
                url=_absolute(url, archive_url),
                source=CATALYST_SOURCE_ANNOUNCEMENT,
            )
        )
    records.sort(key=_newest_first)
    return records


def _newest_first(record: CatalystRecord) -> tuple[bool, float]:
    stamp = record.published_at
    return (stamp is None, -stamp.timestamp() if stamp is not None else 0.0)


def parse_results_calendar(
    payload: bytes, *, fallback_symbol: str, page_url: str
) -> list[EarningsDateRecord]:
    """Read ``/api/event-calendar`` into the meetings whose purpose names a result.

    AGMs, fund raising and the rest are not earnings and are dropped; a row with no readable
    date cannot be a flag and is dropped too. Soonest first.
    """
    rows = _filings_rows(payload, context=f"{fallback_symbol} event calendar")
    records: list[EarningsDateRecord] = []
    for row in rows:
        purpose = _clean(_first(row, ("purpose", "bm_desc", "PURPOSE")))
        if purpose is None or _RESULTS_PURPOSE.search(purpose) is None:
            continue
        event_date = _date(_first(row, ("bm_date", "date", "BM_DATE")))
        if event_date is None:
            continue
        symbol = _clean(_first(row, ("symbol", "SYMBOL"))) or fallback_symbol
        records.append(
            EarningsDateRecord(
                symbol=symbol,
                event_date=event_date,
                purpose=" ".join(purpose.split()),
                url=f"{page_url}{quote(symbol)}",
            )
        )
    records.sort(key=lambda r: r.event_date)
    return records
