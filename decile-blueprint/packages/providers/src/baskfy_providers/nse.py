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
import re
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final, Protocol

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
    CorporateAction,
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
KIND_LISTINGS: Final = "listings"
KIND_CONSTITUENTS: Final = "constituents"

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
#: defines them by rule: `nifty-allcap` is every EQ instrument with a bar, `etf` is every ETF.
#: docs/01 §2.1 lists `nifty-fno` as a universe but NSE publishes it as a derivatives list, not
#: an index constituent file, so it is resolved by the pipeline rather than fetched here.
DERIVED_UNIVERSES: Final[frozenset[str]] = frozenset({"nifty-allcap", "etf", "nifty-fno"})


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
            f"{self._settings.nse_archive_url}/content/indices/ind_{_file_token(index_slug)}list.csv",
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
        """Splits, bonuses, dividends and rights with an ex-date on or after ``since``."""
        payload = self._archived(
            KIND_CORPORATE_ACTIONS,
            since,
            f"{self._settings.nse_base_url}/api/corporates-corporateActions?index=equities",
            extension="json",
            content_type="application/json",
        )
        frame = _read_json(payload, context="corporate actions")
        actions: list[CorporateAction] = []
        for row in frame.iter_rows(named=True):
            symbol = str(_first(row, ("symbol", "SYMBOL")) or "").strip()
            purpose = str(_first(row, ("subject", "purpose", "SUBJECT")) or "")
            ex_date = _date(_first(row, ("exDate", "EX_DATE", "ex_date")))
            if not symbol or ex_date is None or ex_date < since:
                continue
            parsed = parse_corporate_action_purpose(purpose)
            if parsed is None:
                continue
            action_type, ratio_from, ratio_to, amount = parsed
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
        """The NSE listings register (docs/01 §1: 3,524 rows on /listings)."""
        payload = self._archived(
            KIND_LISTINGS,
            dt.date.today(),
            f"{self._settings.nse_archive_url}/content/equities/EQUITY_L.csv",
        )
        frame = _read_csv(payload, context="listings")
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
                    listed_on=_date(_first(row, (" DATE OF LISTING", "DATE OF LISTING"))),
                    face_value=_decimal(_first(row, (" FACE VALUE", "FACE VALUE"))),
                    paid_up_value=_decimal(_first(row, (" PAID UP VALUE", "PAID UP VALUE"))),
                    market_lot=_int(_first(row, (" MARKET LOT", "MARKET LOT"))),
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

    # --- internals ------------------------------------------------------

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
            client = self._require_client()
            self._prime_cookies(client)
            self._throttle()
            return _body(_get(client, url), url)

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
        """
        if self._cookies_primed:
            return
        self._throttle()
        _get(client, self._settings.nse_base_url)
        self._cookies_primed = True

    def _throttle(self) -> None:
        if self._rate_limiter is not None:
            self._rate_limiter.acquire()


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


def _file_token(index_slug: str) -> str:
    """NSE names its constituent files ``ind_nifty50list.csv`` and similar."""
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
    amount = re.search(r"(?:RS\.?|RE\.?)\s*([\d.]+)", text)
    return ("dividend", None, None, _to_decimal(amount.group(1)) if amount else None)


#: Keyword -> parser, checked in order. SPLIT precedes BONUS because NSE publishes combined
#: "SPLIT AND BONUS" purposes, and the split leg is the one that changes the face value.
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


def parse_corporate_action_purpose(purpose: str) -> ParsedAction | None:
    """Read NSE's free-text "purpose" field into a typed action.

    NSE does not publish a structured action type; it publishes strings like
    ``"FACE VALUE SPLIT FROM RS.10/- TO RE.1/-"`` or ``"BONUS 4:1"``. The ratio convention that
    comes out of here is docs/04's: split 10:1 -> ``from=10, to=1``; bonus 4:1 -> ``from=4, to=1``.

    Returns ``None`` for purposes we do not model — AGMs, name changes, board meetings — which is
    the overwhelming majority of rows in that file. Guessing at an unrecognised purpose would put
    a wrong adjustment factor into every price before its ex-date, so an unreadable purpose is
    dropped rather than approximated.
    """
    text = purpose.upper().strip()
    if not text:
        return None
    for keyword, parse in _PURPOSE_PARSERS:
        if keyword in text:
            return parse(text)
    return None


def _to_decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text)
    except InvalidOperation:
        return None
