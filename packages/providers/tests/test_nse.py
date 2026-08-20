"""NSEProvider (Prompt 2 deliverable 3).

Every test drives a fake HTTP client through :class:`NSERuntime`, so nothing reaches the network
(acceptance criterion 1) while the cookie priming, the archive-before-parse ordering and every
parser are still exercised against realistic file shapes.
"""

from __future__ import annotations

import datetime as dt
import io
import zipfile
from collections.abc import Mapping
from decimal import Decimal

import pytest

from decile_providers.archive import LocalRawArchive, archive_key
from decile_providers.errors import ProviderUnavailable, UnexpectedPayload, UpstreamUnavailable
from decile_providers.nse import (
    KIND_BHAVCOPY,
    NSEProvider,
    NSERuntime,
    parse_corporate_action_purpose,
)
from decile_providers.ports import REFERENCE_CAPABILITIES, Capability
from decile_providers.records import BHAVCOPY_SCHEMA
from decile_providers.retry import RetryPolicy
from decile_providers.settings import ProviderSettings

ON = dt.date(2026, 8, 18)


class FakeResponse:
    def __init__(self, content: bytes, status_code: int = 200) -> None:
        self._content = content
        self._status = status_code

    @property
    def status_code(self) -> int:
        return self._status

    @property
    def content(self) -> bytes:
        return self._content


class FakeHttpClient:
    """Serves canned bodies by URL substring, and records the order of every request."""

    def __init__(
        self,
        routes: Mapping[str, bytes] | None = None,
        *,
        status: int = 200,
        raises: Exception | None = None,
    ) -> None:
        self._routes = dict(routes or {})
        self._status = status
        self._raises = raises
        self.requests: list[str] = []

    def get(self, url: str, *, headers: Mapping[str, str] | None = None) -> FakeResponse:
        del headers
        self.requests.append(url)
        if self._raises is not None:
            raise self._raises
        for fragment, body in self._routes.items():
            if fragment in url:
                return FakeResponse(body, self._status)
        return FakeResponse(b"", self._status)


class InertLimiter:
    def __init__(self) -> None:
        self.acquisitions = 0

    def acquire(self, tokens: float = 1.0) -> float:
        del tokens
        self.acquisitions += 1
        return 0.0


def build(
    settings: ProviderSettings,
    archive: LocalRawArchive,
    client: FakeHttpClient,
    limiter: InertLimiter | None = None,
) -> NSEProvider:
    return NSEProvider(
        settings,
        archive,
        NSERuntime(
            client=client,
            rate_limiter=limiter or InertLimiter(),
            retry_policy=RetryPolicy(max_attempts=1, base_seconds=0.01),
        ),
    )


class TestCapabilities:
    def test_it_offers_only_reference_data(self, settings: ProviderSettings) -> None:
        """docs/09's table: NSE provides ReferenceProvider. Bars come from Kite."""
        provider = NSEProvider(settings)
        assert provider.capabilities() == REFERENCE_CAPABILITIES
        assert Capability.DAILY_BARS not in provider.capabilities()


class TestHealth:
    def test_without_an_archive_it_is_unavailable(self, settings: ProviderSettings) -> None:
        health = NSEProvider(settings).check()
        assert health.available is False
        assert "archive" in health.detail

    def test_fully_wired_is_available(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        assert build(settings, archive, FakeHttpClient()).check().available is True


class TestBrowserSession:
    """docs/09: "public files need a browser-like session (cookie priming)"."""

    def test_the_site_root_is_visited_before_the_archive_file(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        client = FakeHttpClient({"EQUITY_L.csv": _listings_csv()})
        build(settings, archive, client).listings()
        assert client.requests[0] == settings.nse_base_url
        assert "EQUITY_L.csv" in client.requests[1]

    def test_cookies_are_primed_once_per_provider_not_once_per_request(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        """Re-priming on every file is itself a traffic pattern that gets throttled."""
        client = FakeHttpClient(
            {"EQUITY_L.csv": _listings_csv(), "ind_close_all": _index_snapshot_csv()}
        )
        provider = build(settings, archive, client)
        provider.listings()
        provider.index_snapshots(ON)
        assert client.requests.count(settings.nse_base_url) == 1

    def test_every_request_takes_a_rate_limit_token(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        """docs/09: NSE's public files are rate-sensitive."""
        limiter = InertLimiter()
        client = FakeHttpClient({"EQUITY_L.csv": _listings_csv()})
        build(settings, archive, client, limiter).listings()
        # One for the cookie prime, one for the file.
        assert limiter.acquisitions == 2


class TestArchiveFirst:
    def test_the_raw_file_is_archived_before_parsing(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        client = FakeHttpClient({"BhavCopy": _bhavcopy_zip()})
        build(settings, archive, client).bhavcopy(ON)
        assert archive.exists(archive_key(KIND_BHAVCOPY, ON, "zip"))

    def test_a_second_call_parses_the_archive_without_refetching(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        """docs/09: "Never re-fetch to re-parse"."""
        client = FakeHttpClient({"BhavCopy": _bhavcopy_zip()})
        provider = build(settings, archive, client)
        provider.bhavcopy(ON)
        before = len(client.requests)
        provider.bhavcopy(ON)
        assert len(client.requests) == before

    def test_without_an_archive_it_refuses_rather_than_parsing_in_memory(
        self, settings: ProviderSettings
    ) -> None:
        provider = NSEProvider(settings, None, NSERuntime(client=FakeHttpClient()))
        with pytest.raises(ProviderUnavailable, match="archive"):
            provider.bhavcopy(ON)


class TestBhavcopy:
    def test_it_conforms_to_the_schema(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        frame = build(settings, archive, FakeHttpClient({"BhavCopy": _bhavcopy_zip()})).bhavcopy(ON)
        assert dict(frame.schema) == BHAVCOPY_SCHEMA

    def test_series_and_circuit_bands_are_carried(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        """docs/09 §"Provider ports": "bhavcopy(on) ... incl. circuit bands, series"."""
        frame = build(settings, archive, FakeHttpClient({"BhavCopy": _bhavcopy_zip()})).bhavcopy(ON)
        row = frame.filter(frame["symbol"] == "SBIN").to_dicts()[0]
        assert row["series"] == "EQ"
        assert row["upper_circuit"] == Decimal("880.0000")
        assert row["lower_circuit"] == Decimal("720.0000")

    def test_turnover_is_read_as_rupees(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        """docs/13 §2 finding 5: `volume` in the export is traded value, not a share count."""
        frame = build(settings, archive, FakeHttpClient({"BhavCopy": _bhavcopy_zip()})).bhavcopy(ON)
        row = frame.filter(frame["symbol"] == "SBIN").to_dicts()[0]
        assert row["turnover"] == Decimal("8000000.00")

    def test_the_legacy_column_vocabulary_is_accepted(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        """NSE renamed every bhavcopy column; a 15-year backfill spans both spellings."""
        legacy = (
            b"SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TOTALTRADES\n"
            b"SBIN,EQ,790,805,788,800,795,10000,8000000,1200\n"
        )
        frame = build(settings, archive, FakeHttpClient({"BhavCopy": legacy})).bhavcopy(ON)
        assert frame["symbol"].to_list() == ["SBIN"]
        assert frame["close"][0] == Decimal("800.0000")

    def test_an_empty_file_is_an_error_not_an_empty_frame(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        """A day with no rows is a publication failure; docs/09 leaves the verdict to the QA
        gate, but the parser must not pretend it read a valid file."""
        header_only = b"TckrSymb,SctySrs,OpnPric,HghPric,LwPric,ClsPric\n"
        with pytest.raises(UnexpectedPayload):
            build(settings, archive, FakeHttpClient({"BhavCopy": header_only})).bhavcopy(ON)


class TestListings:
    def test_rows_are_mapped(self, settings: ProviderSettings, archive: LocalRawArchive) -> None:
        records = build(settings, archive, FakeHttpClient({"EQUITY_L": _listings_csv()})).listings()
        assert [r.symbol for r in records] == ["SBIN", "CUPID"]
        assert records[0].listed_on == dt.date(1997, 3, 1)
        assert records[0].face_value == Decimal("1")

    def test_the_leading_space_column_names_nse_publishes_are_handled(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        """NSE's EQUITY_L.csv really does ship columns named " SERIES" with a leading space."""
        records = build(settings, archive, FakeHttpClient({"EQUITY_L": _listings_csv()})).listings()
        assert records[0].series == "EQ"


class TestIndexSnapshots:
    def test_levels_and_fundamentals_are_read(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        client = FakeHttpClient({"ind_close_all": _index_snapshot_csv()})
        snapshots = build(settings, archive, client).index_snapshots(ON)
        nifty = next(s for s in snapshots if s.index_slug == "nifty-50")
        assert nifty.level == Decimal("24500.35")
        assert nifty.pe == Decimal("22.4")

    def test_a_derived_index_with_no_fundamentals_yields_none_not_zero(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        """docs/01 §7: India VIX and inverse indices render PE/PB/DivYield as `-`. Zero is a lie."""
        client = FakeHttpClient({"ind_close_all": _index_snapshot_csv()})
        snapshots = build(settings, archive, client).index_snapshots(ON)
        vix = next(s for s in snapshots if s.index_slug == "INDIA VIX")
        assert vix.pe is None
        assert vix.pb is None


class TestIndexConstituents:
    def test_symbols_are_returned(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        client = FakeHttpClient(
            {"ind_nifty50list": b"Company Name,Industry,Symbol\nSBI,Bank,SBIN\n"}
        )
        assert build(settings, archive, client).index_constituents("nifty-50", ON) == ["SBIN"]

    def test_a_derived_universe_is_refused_with_an_explanation(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        """docs/06 §"Step 2": nifty-allcap and etf are defined by rule, not by an NSE file."""
        provider = build(settings, archive, FakeHttpClient())
        with pytest.raises(UnexpectedPayload, match="derived by rule"):
            provider.index_constituents("nifty-allcap", ON)

    def test_an_unknown_universe_is_refused(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        provider = build(settings, archive, FakeHttpClient())
        with pytest.raises(UnexpectedPayload, match="unknown universe"):
            provider.index_constituents("nifty-4000", ON)

    def test_a_file_without_a_symbol_column_is_an_error(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        client = FakeHttpClient({"ind_nifty50list": b"Company Name,Industry\nSBI,Bank\n"})
        with pytest.raises(UnexpectedPayload, match="expected columns"):
            build(settings, archive, client).index_constituents("nifty-50", ON)


class TestHttpFailures:
    def test_a_5xx_is_transient(self, settings: ProviderSettings, archive: LocalRawArchive) -> None:
        client = FakeHttpClient({"EQUITY_L": b"x"}, status=503)
        with pytest.raises(Exception) as raised:
            build(settings, archive, client).listings()
        assert "503" in str(raised.value) or "gave up" in str(raised.value)

    def test_a_connection_error_is_translated(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        client = FakeHttpClient(raises=OSError("connection reset"))
        with pytest.raises(Exception) as raised:
            build(settings, archive, client).listings()
        assert "gave up" in str(raised.value) or isinstance(raised.value, UpstreamUnavailable)


class TestCorporateActionPurposes:
    """NSE publishes free text, not a structured type. docs/04 fixes the ratio convention."""

    def test_a_face_value_split_is_read_as_a_split(self) -> None:
        parsed = parse_corporate_action_purpose("FACE VALUE SPLIT FROM RS.10/- TO RE.1/-")
        assert parsed == ("split", Decimal("10"), Decimal("1"), None)

    def test_a_bonus_ratio_keeps_the_documented_orientation(self) -> None:
        """docs/04: bonus 4:1 -> ratio_from=4, ratio_to=1."""
        assert parse_corporate_action_purpose("BONUS 4:1") == (
            "bonus",
            Decimal("4"),
            Decimal("1"),
            None,
        )

    def test_a_cash_dividend_carries_an_amount(self) -> None:
        parsed = parse_corporate_action_purpose("DIVIDEND - RS.12.50 PER SHARE")
        assert parsed == ("dividend", None, None, Decimal("12.50"))

    def test_rights_are_recognised_even_without_a_ratio(self) -> None:
        assert parse_corporate_action_purpose("RIGHTS ISSUE") == ("rights", None, None, None)

    def test_a_demerger_is_recognised(self) -> None:
        assert parse_corporate_action_purpose("SCHEME OF DEMERGER") == (
            "demerger",
            None,
            None,
            None,
        )

    @pytest.mark.parametrize(
        "purpose",
        [
            "ANNUAL GENERAL MEETING",
            "CHANGE IN NAME",
            "BOARD MEETING INTIMATION",
            "",
        ],
    )
    def test_an_unmodelled_purpose_is_dropped_rather_than_guessed(self, purpose: str) -> None:
        """A wrong action puts a wrong adjustment factor on every price before its ex-date."""
        assert parse_corporate_action_purpose(purpose) is None


def _listings_csv() -> bytes:
    return (
        b"SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT,"
        b" ISIN NUMBER, FACE VALUE\n"
        b"SBIN,STATE BANK OF INDIA,EQ,01-MAR-1997,1,1,INE062A01020,1\n"
        b"CUPID,CUPID LIMITED,EQ,11-OCT-2017,1,1,INE509G01020,1\n"
    )


def _index_snapshot_csv() -> bytes:
    return (
        b"Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,"
        b"Closing Index Value,Points Change,Change(%),Volume,Turnover (Rs. Cr.),P/E,P/B,Div Yield\n"
        b"NIFTY 50,18-08-2026,24400,24550,24380,24500.35,100.35,0.41,100,5000,22.4,3.9,1.2\n"
        b"INDIA VIX,18-08-2026,12,13,11,12.5,0.5,4.0,0,0,-,-,-\n"
    )


def _bhavcopy_zip() -> bytes:
    csv = (
        b"TckrSymb,SctySrs,OpnPric,HghPric,LwPric,ClsPric,PrvsClsgPric,TtlTradgVol,TtlTrfVal,"
        b"TtlNbOfTxsExctd,UpprBookgPric,LwrBookgPric\n"
        b"SBIN,EQ,790,805,788,800,795,10000,8000000,1200,880,720\n"
        b"CUPID,EQ,273,285.7,257.21,284.03,280,5000,1420150,400,340.836,227.224\n"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("BhavCopy_NSE_CM_0_0_0_20260818_F_0000.csv", csv)
    return buffer.getvalue()
