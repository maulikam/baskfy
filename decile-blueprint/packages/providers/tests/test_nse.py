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

from baskfy_providers.archive import LocalRawArchive, archive_key
from baskfy_providers.errors import ProviderUnavailable, UnexpectedPayload, UpstreamUnavailable
from baskfy_providers.nse import (
    KIND_BHAVCOPY,
    NSEProvider,
    NSERuntime,
    parse_corporate_action_purpose,
    parse_equity_quote,
)
from baskfy_providers.ports import REFERENCE_CAPABILITIES, Capability
from baskfy_providers.records import BHAVCOPY_SCHEMA
from baskfy_providers.retry import RetryPolicy
from baskfy_providers.settings import ProviderSettings

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

    def test_the_archived_file_from_the_box_parses_to_the_published_close(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        """SW16: the NSE path was not the cause of the 1/14-scale rows.

        This is the first row of ``nse/index-snapshot/2026-08-27.csv`` as archived on the box —
        mixed-case name, ``-.48`` with no leading zero — and it parses to the level the box holds
        for that day (24,090.85), on the ``nifty-50`` slug. The 1,128.60 the box held for
        2026-08-18 was the fixture builder's random walk, which the writer's guard now refuses
        (``services/worker/tests/test_snapshots.py``).
        """
        on = dt.date(2026, 8, 27)
        client = FakeHttpClient({"ind_close_all_27082026": _archived_index_row_from_the_box()})
        snapshots = build(settings, archive, client).index_snapshots(on)
        nifty = next(s for s in snapshots if s.index_slug == "nifty-50")
        assert nifty.level == Decimal("24090.85")
        assert nifty.change_abs == Decimal("-116.9")
        assert nifty.change_pct == Decimal("-0.48")
        assert nifty.pe == Decimal("20.37")

    def test_a_file_dated_for_another_session_is_refused(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        """SW16: the file fetched for a date must say that date in its own ``Index Date``.

        A file carrying another session under this archive key would land every level on the
        wrong day and look plausible — structural damage, which docs/09 makes the parser refuse.
        """
        client = FakeHttpClient({"ind_close_all": _index_snapshot_csv()})  # dated 18-08-2026
        with pytest.raises(UnexpectedPayload, match=r"2026-08-19.*2026-08-18"):
            build(settings, archive, client).index_snapshots(dt.date(2026, 8, 19))


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


class TestEquityFundamentals:
    """docs/05 §14. NSE retired ``/api/quote-equity`` in its Next.js migration and now serves
    ``GetQuoteApi?functionName=getSymbolData``; the retired shape must still parse out of the
    archive, because archived bytes are permanent (docs/09)."""

    def test_issued_size_times_last_price_is_marketcap_in_crore(self) -> None:
        record = parse_equity_quote(_get_symbol_data_json(), on=ON, fallback_symbol="INFY")
        assert record is not None
        assert record.symbol == "INFY"
        assert record.shares_outstanding == 4_058_056_597
        # 4_058_056_597 * 1144 / 1e7, half-up.
        assert record.marketcap_cr == 464_242
        assert record.pe == Decimal("15.12")

    def test_the_retired_payload_still_parses_out_of_the_archive(self) -> None:
        """A pre-migration file must never become unreadable — that is why we archive bytes."""
        record = parse_equity_quote(_quote_equity_json(), on=ON, fallback_symbol="INFY")
        assert record is not None
        assert record.shares_outstanding == 4_148_506_959
        assert record.marketcap_cr == 767578
        assert record.pe == Decimal("24.5")

    def test_pb_and_div_yield_are_null_because_the_live_payload_omits_them(self) -> None:
        """An absent number must look absent. Carrying old field names forward would make a
        NULL look like a fetched zero."""
        record = parse_equity_quote(_get_symbol_data_json(), on=ON, fallback_symbol="INFY")
        assert record is not None
        assert record.pb is None
        assert record.div_yield is None

    def test_a_wrong_series_is_not_a_row(self) -> None:
        """NSE answers a wrong series with 200 and an empty list, not an error."""
        empty = b'{"equityResponse":[]}'
        assert parse_equity_quote(empty, on=ON, fallback_symbol="INFY") is None

    def test_an_entry_whose_sections_are_all_null_is_not_a_row(self) -> None:
        """The literal shape NSE returns for a name it does not quote — captured from DSFCL
        on 2026-08-18. Every section is JSON ``null``, not absent, so a parser that only
        guarded against missing keys would build a row of zeroes out of it."""
        payload = (
            b'{"equityResponse":[{"orderBook":null,"metaData":null,"tradeInfo":null,'
            b'"priceInfo":null,"secInfo":null,"lastUpdateTime":null}]}'
        )
        assert parse_equity_quote(payload, on=ON, fallback_symbol="DSFCL") is None

    def test_nse_total_marketcap_is_used_when_shares_are_missing(self) -> None:
        payload = (
            b'{"equityResponse":[{"metaData":{"symbol":"X"},'
            b'"tradeInfo":{"totalMarketCap":4642416746968},"secInfo":{"pdSymbolPe":"15.12"}}]}'
        )
        record = parse_equity_quote(payload, on=ON, fallback_symbol="X")
        assert record is not None
        assert record.shares_outstanding is None
        assert record.marketcap_cr == 464_242

    def test_a_missing_quote_is_skipped_not_zero(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        client = FakeHttpClient(status=404)
        records = build(settings, archive, client).equity_fundamentals(ON, ["NOSUCH"])
        assert records == []

    def test_quotes_are_archived_per_symbol_and_series(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        client = FakeHttpClient({"getSymbolData": _get_symbol_data_json()})
        records = build(settings, archive, client).equity_fundamentals(
            ON, ["INFY"], series_by_symbol={"INFY": "EQ"}
        )
        assert len(records) == 1
        assert archive.exists(archive_key("equity-fundamentals/INFY-EQ", ON, "json"))

    def test_a_hinted_series_costs_exactly_one_request(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        """The hint exists to avoid a lookup round trip; if it does not, it is pointless."""
        client = FakeHttpClient({"getSymbolData": _get_symbol_data_json()})
        build(settings, archive, client).equity_fundamentals(
            ON, ["INFY"], series_by_symbol={"INFY": "EQ"}
        )
        quote_calls = [u for u in client.requests if "GetQuoteApi" in u]
        assert len(quote_calls) == 1
        assert "getMetaData" not in quote_calls[0]

    def test_a_stale_hint_falls_back_to_the_series_nse_reports(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        """A wrong hint must cost a round trip, never a NULL row."""
        client = FakeHttpClient(
            {
                "series=EQ": b'{"equityResponse":[]}',
                "getMetaData": b'{"symbol":"X","activeSeries":["BE"]}',
                "series=BE": _get_symbol_data_json(),
            }
        )
        records = build(settings, archive, client).equity_fundamentals(
            ON, ["X"], series_by_symbol={"X": "EQ"}
        )
        assert len(records) == 1
        assert any("getMetaData" in u for u in client.requests)

    def test_no_hint_asks_nse_which_series_it_quotes(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        client = FakeHttpClient(
            {
                "getMetaData": b'{"symbol":"X","activeSeries":["BE"]}',
                "series=BE": _get_symbol_data_json(),
            }
        )
        records = build(settings, archive, client).equity_fundamentals(ON, ["X"])
        assert len(records) == 1


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


def _archived_index_row_from_the_box() -> bytes:
    """Header and first row of the box's ``nse/index-snapshot/2026-08-27.csv``, verbatim."""
    return (
        b"Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,"
        b"Closing Index Value,Points Change,Change(%),Volume,Turnover (Rs. Cr.),P/E,P/B,Div Yield\n"
        b"Nifty 50,27-08-2026,24277.6,24297.45,24090.85,24090.85,-116.9,-.48,323419647,"
        b"24757.82,20.37,2.92,1.17\n"
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


def _get_symbol_data_json() -> bytes:
    """A trimmed copy of the live ``getSymbolData`` payload for INFY (captured 25 Aug 2026)."""
    return (
        b'{"equityResponse":[{"orderBook":{"lastPrice":1144},'
        b'"metaData":{"symbol":"INFY","series":"EQ","closePrice":1144},'
        b'"tradeInfo":{"issuedSize":4058056597,"lastPrice":1144,"faceValue":5,'
        b'"totalMarketCap":4642416746968},'
        b'"priceInfo":{"yearHigh":1728,"yearLow":982.4},'
        b'"secInfo":{"secStatus":"Listed","pdSectorPe":"14.69","pdSymbolPe":"15.12"}}],'
        b'"lastUpdateTime":"25-Aug-2026 16:00:00"}'
    )


def _quote_equity_json() -> bytes:
    return (
        b'{"info":{"symbol":"INFY"},"securityInfo":{"issuedSize":4148506959},'
        b'"priceInfo":{"lastPrice":1850.25,"pE":24.5,"pB":7.1},"metadata":{}}'
    )
