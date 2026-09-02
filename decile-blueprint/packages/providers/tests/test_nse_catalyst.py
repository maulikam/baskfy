"""SW11B: the catalyst feed's two NSE reads (STANDING-ANSWERS A3).

Every test drives the fake HTTP client of ``test_nse.py`` through :class:`NSERuntime` over the
recorded payloads in ``fixtures/nse/`` — no network, the cookie prime, the limiter and the
archive-before-parse ordering all exercised. What is asserted is the spec, not the code:

* a record is a **headline, a stamp and a link** — never the filing's text (A3, Track C §7);
* a row with no attachment is dropped, a row with no stamp is kept and sorts last;
* a URL keeps its query string, because it is ``sw_catalyst``'s uniqueness key;
* the calendar yields result meetings only, soonest first, each linking to the exchange's page;
* a symbol NSE does not know is absence, and every other failure is the provider's own error.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from pathlib import Path

import pytest

from baskfy_providers.archive import LocalRawArchive, archive_key
from baskfy_providers.errors import ProviderError, UnexpectedPayload
from baskfy_providers.nse import (
    HEADLINE_MAX_CHARS,
    IST,
    KIND_ANNOUNCEMENTS,
    KIND_EVENT_CALENDAR,
    NSEProvider,
    NSERuntime,
    parse_announcements,
    parse_results_calendar,
)
from baskfy_providers.retry import RetryPolicy
from baskfy_providers.settings import ProviderSettings

ON = dt.date(2026, 9, 2)
FIXTURES = Path(__file__).parent / "fixtures" / "nse"


def _recorded(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


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
        return FakeResponse(b"[]", self._status)


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


def _routes() -> dict[str, bytes]:
    return {
        "corporate-announcements": _recorded("announcements-INFY.json"),
        "event-calendar": _recorded("event-calendar-INFY.json"),
    }


class TestAnnouncements:
    def test_an_announcement_is_a_headline_a_stamp_and_a_link(self) -> None:
        records = parse_announcements(
            _recorded("announcements-INFY.json"),
            fallback_symbol="INFY",
            archive_url="https://nsearchives.nseindia.com",
        )
        newest = records[0]
        assert newest.symbol == "INFY"
        assert newest.published_at == dt.datetime(2026, 9, 2, 8, 41, 5, tzinfo=IST)
        assert newest.url.startswith("https://nsearchives.nseindia.com/corporate/INFY_02092026")
        assert newest.headline.startswith("Press Release - Infosys and a European bank")
        assert set(newest.model_dump()) == {"symbol", "headline", "published_at", "url", "source"}

    def test_the_headline_is_capped_so_the_filing_text_is_never_carried(self) -> None:
        """A3: link out, do not reproduce text. The cap is what makes a headline a headline."""
        records = parse_announcements(
            _recorded("announcements-INFY.json"),
            fallback_symbol="INFY",
            archive_url="https://nsearchives.nseindia.com",
        )
        assert all(len(r.headline) <= HEADLINE_MAX_CHARS for r in records)
        assert records[0].headline.endswith("…")

    def test_an_announcement_without_an_attachment_is_dropped_not_invented(self) -> None:
        records = parse_announcements(
            _recorded("announcements-INFY.json"),
            fallback_symbol="INFY",
            archive_url="https://nsearchives.nseindia.com",
        )
        assert "Takeover" not in " ".join(r.headline for r in records)
        assert len(records) == 3

    def test_an_announcement_without_a_timestamp_is_kept_and_sorts_last(self) -> None:
        records = parse_announcements(
            _recorded("announcements-INFY.json"),
            fallback_symbol="INFY",
            archive_url="https://nsearchives.nseindia.com",
        )
        assert [r.published_at is None for r in records] == [False, False, True]
        assert records[-1].headline == "Analysts/Institutional Investor Meet/Con. Call Updates"

    def test_a_url_keeps_its_query_string_verbatim(self) -> None:
        """The URL is `sw_catalyst`'s uniqueness key; normalising it is how two filings collide."""
        records = parse_announcements(
            _recorded("announcements-INFY.json"),
            fallback_symbol="INFY",
            archive_url="https://nsearchives.nseindia.com",
        )
        assert records[0].url.endswith("INFY_02092026084105_PR.pdf?x=1&y=2")

    def test_a_relative_attachment_is_joined_to_the_archive_host(self) -> None:
        payload = (
            b'[{"symbol":"X","desc":"Updates","attchmntFile":"/corporate/X.pdf",'
            b'"an_dt":"01-Sep-2026 10:00:00"}]'
        )
        records = parse_announcements(
            payload, fallback_symbol="X", archive_url="https://nsearchives.nseindia.com"
        )
        assert records[0].url == "https://nsearchives.nseindia.com/corporate/X.pdf"

    def test_an_empty_an_dt_beside_a_filled_sort_date_keeps_the_stamp(self) -> None:
        payload = (
            b'[{"symbol":"X","desc":"Updates","attchmntFile":"https://a/x.pdf",'
            b'"an_dt":"","sort_date":"2026-09-01 10:00:00"}]'
        )
        records = parse_announcements(payload, fallback_symbol="X", archive_url="https://a")
        assert records[0].published_at == dt.datetime(2026, 9, 1, 10, 0, tzinfo=IST)

    def test_an_empty_list_is_no_announcements_not_an_error(self) -> None:
        assert parse_announcements(b"[]", fallback_symbol="X", archive_url="https://a") == []

    def test_a_body_that_is_not_a_list_is_an_unexpected_payload(self) -> None:
        with pytest.raises(UnexpectedPayload):
            parse_announcements(b'{"error":"x"}', fallback_symbol="X", archive_url="https://a")
        with pytest.raises(UnexpectedPayload):
            parse_announcements(b"<html>", fallback_symbol="X", archive_url="https://a")


class TestAnnouncementsThroughTheProvider:
    def test_every_symbol_is_one_archived_request_under_the_limiter(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        limiter = InertLimiter()
        client = FakeHttpClient(_routes())
        provider = build(settings, archive, client, limiter)
        records = provider.announcements(["INFY", "tcs "], on=ON)
        # The site root once, then one request per symbol; a token for each.
        assert client.requests[0] == settings.nse_base_url
        assert [u for u in client.requests if "corporate-announcements" in u] == [
            f"{settings.nse_base_url}/api/corporate-announcements?index=equities&symbol=INFY",
            f"{settings.nse_base_url}/api/corporate-announcements?index=equities&symbol=TCS",
        ]
        assert limiter.acquisitions == 3
        assert archive.exists(archive_key(f"{KIND_ANNOUNCEMENTS}/INFY", ON, "json"))
        assert {r.symbol for r in records} == {"INFY"}

    def test_a_second_run_the_same_morning_parses_the_archive(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        client = FakeHttpClient(_routes())
        provider = build(settings, archive, client)
        provider.announcements(["INFY"], on=ON)
        before = len(client.requests)
        provider.announcements(["INFY"], on=ON)
        assert len(client.requests) == before

    def test_a_symbol_nse_does_not_know_is_absence(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        """A renamed symbol answers 404. No row, no error — the same rule as a missing quote."""
        client = FakeHttpClient(status=404)
        assert build(settings, archive, client).announcements(["OLDNAME"], on=ON) == []

    def test_a_rate_limit_or_an_outage_is_the_provider_error_not_a_swallow(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        for status in (429, 503):
            client = FakeHttpClient({"corporate-announcements": b"[]"}, status=status)
            with pytest.raises(ProviderError):
                build(settings, archive, client).announcements(["INFY"], on=ON)

    def test_a_forbidden_answer_is_loud(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        """403 is how NSE's edge refuses a session it does not like; that is not absence."""
        client = FakeHttpClient(status=403)
        with pytest.raises(UnexpectedPayload):
            build(settings, archive, client).announcements(["INFY"], on=ON)


class TestResultsCalendar:
    def test_only_result_meetings_are_earnings_dates_soonest_first(self) -> None:
        records = parse_results_calendar(
            _recorded("event-calendar-INFY.json"),
            fallback_symbol="INFY",
            page_url="https://www.nseindia.com/companies-listing/corporate-filings-event-calendar?symbol=",
        )
        assert [(r.event_date, r.purpose) for r in records] == [
            (dt.date(2026, 4, 16), "Financial Results/Dividend"),
            (dt.date(2026, 10, 15), "Financial Results"),
        ]
        assert records[0].url.endswith("corporate-filings-event-calendar?symbol=INFY")

    def test_the_results_calendar_goes_through_the_provider_archived_and_limited(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        limiter = InertLimiter()
        client = FakeHttpClient(_routes())
        records = build(settings, archive, client, limiter).results_calendar(["INFY"], on=ON)
        assert [u for u in client.requests if "event-calendar" in u] == [
            f"{settings.nse_base_url}/api/event-calendar?index=equities&symbol=INFY"
        ]
        assert limiter.acquisitions == 2
        assert archive.exists(archive_key(f"{KIND_EVENT_CALENDAR}/INFY", ON, "json"))
        assert [r.event_date for r in records] == [dt.date(2026, 4, 16), dt.date(2026, 10, 15)]

    def test_an_earnings_date_the_calendar_cannot_date_is_dropped(self) -> None:
        records = parse_results_calendar(
            _recorded("event-calendar-INFY.json"), fallback_symbol="INFY", page_url="https://p?s="
        )
        assert len(records) == 2

    def test_a_results_calendar_outage_is_the_provider_error(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        client = FakeHttpClient(raises=OSError("connection reset"))
        with pytest.raises(ProviderError):
            build(settings, archive, client).results_calendar(["INFY"], on=ON)
