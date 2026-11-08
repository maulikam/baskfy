"""NSEProvider.fo_ban_list — the F&O ban list for the next session (docs/fno/02 Track C §9, FO2).

Asserts the spec: the file names the session it is for and is refused for any other (the SW16
rule), it is archived before it is parsed and keyed by the session it *says* it is for, an
archived answer is not re-fetched, NIL is an empty list, and a file that cannot be read in full is
refused rather than read in part. Nothing reaches the network.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import pytest

from baskfy_providers.archive import LocalRawArchive, archive_key
from baskfy_providers.errors import UnexpectedPayload
from baskfy_providers.nse import (
    FO_BAN_LIST_PATH,
    KIND_FO_BAN_LIST,
    NSEProvider,
    NSERuntime,
    parse_fo_ban_list,
)
from baskfy_providers.retry import RetryPolicy
from baskfy_providers.settings import ProviderSettings

FIXTURE: Final = Path(__file__).parent / "fixtures" / "nse" / "fo_secban-2026-09-25.csv"
SESSION: Final = dt.date(2026, 9, 25)


class FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.status_code = 200
        self.content = content


class FakeHttpClient:
    """Serves canned bodies by URL substring and records every request."""

    def __init__(self, routes: Mapping[str, bytes]) -> None:
        self._routes = dict(routes)
        self.requests: list[str] = []

    def get(self, url: str, *, headers: Mapping[str, str] | None = None) -> FakeResponse:
        del headers
        self.requests.append(url)
        for fragment, body in self._routes.items():
            if fragment in url:
                return FakeResponse(body)
        return FakeResponse(b"")


class CountingLimiter:
    def __init__(self) -> None:
        self.acquired = 0

    def acquire(self, tokens: float = 1.0) -> float:
        del tokens
        self.acquired += 1
        return 0.0


def build(
    settings: ProviderSettings,
    archive: LocalRawArchive,
    client: FakeHttpClient,
    limiter: CountingLimiter | None = None,
) -> NSEProvider:
    return NSEProvider(
        settings,
        archive,
        NSERuntime(
            client=client,
            rate_limiter=limiter or CountingLimiter(),
            retry_policy=RetryPolicy(max_attempts=1, base_seconds=0.01),
        ),
    )


def _ban_file(stamp: str, *rows: str) -> bytes:
    body = f"Securities in Ban For Trade Date {stamp}:\n" + "".join(f"{r}\n" for r in rows)
    return body.encode()


class TestTheRecordedFile:
    def test_it_reads_the_four_securities_fo0_saw(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        client = FakeHttpClient({"fo_secban": FIXTURE.read_bytes()})
        banned = build(settings, archive, client).fo_ban_list(SESSION)
        assert banned == ["KAYNES", "LICHSGFIN", "MANAPPURAM", "SAIL"]

    def test_the_url_is_the_ban_file_behind_the_limiter_and_cookie_priming(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        client = FakeHttpClient({"fo_secban": FIXTURE.read_bytes()})
        limiter = CountingLimiter()
        build(settings, archive, client, limiter).fo_ban_list(SESSION)
        assert client.requests[-1] == f"{settings.nse_archive_url}{FO_BAN_LIST_PATH}"
        assert client.requests[-1].endswith("/content/fo/fo_secban.csv")
        # One cookie-priming visit to the site root, then the file; each one throttled.
        assert client.requests[0] == settings.nse_base_url
        assert limiter.acquired == len(client.requests) == 2

    def test_it_is_archived_under_its_session_before_parsing_and_never_refetched(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        client = FakeHttpClient({"fo_secban": FIXTURE.read_bytes()})
        provider = build(settings, archive, client)
        first = provider.fo_ban_list(SESSION)
        key = archive_key(KIND_FO_BAN_LIST, SESSION, "csv")
        assert archive.exists(key)
        assert archive.get(key) == FIXTURE.read_bytes()
        asked = len(client.requests)
        assert provider.fo_ban_list(SESSION) == first
        assert len(client.requests) == asked, "an archived session must not ask NSE again"


class TestTheWrongDateRefusal:
    def test_todays_list_served_for_tomorrow_is_refused(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        client = FakeHttpClient({"fo_secban": _ban_file("24-SEP-2026", "1,IDEA")})
        with pytest.raises(UnexpectedPayload, match="wrong file"):
            build(settings, archive, client).fo_ban_list(SESSION)
        # Archived under the session it names (true), never under the one asked for.
        assert archive.exists(archive_key(KIND_FO_BAN_LIST, dt.date(2026, 9, 24), "csv"))
        assert not archive.exists(archive_key(KIND_FO_BAN_LIST, SESSION, "csv"))

    def test_a_refused_session_can_be_asked_again_once_nse_publishes(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        early = FakeHttpClient({"fo_secban": _ban_file("24-SEP-2026", "1,IDEA")})
        with pytest.raises(UnexpectedPayload):
            build(settings, archive, early).fo_ban_list(SESSION)
        later = FakeHttpClient({"fo_secban": FIXTURE.read_bytes()})
        assert build(settings, archive, later).fo_ban_list(SESSION) == [
            "KAYNES",
            "LICHSGFIN",
            "MANAPPURAM",
            "SAIL",
        ]


class TestTheParser:
    def test_nil_is_an_empty_list(self) -> None:
        stamped, symbols = parse_fo_ban_list(b"Securities in Ban For Trade Date 25-SEP-2026: NIL\n")
        assert (stamped, symbols) == (SESSION, [])

    def test_no_rows_is_an_empty_list(self) -> None:
        assert parse_fo_ban_list(_ban_file("25-SEP-2026")) == (SESSION, [])

    def test_rows_are_sorted_upper_cased_and_deduplicated(self) -> None:
        payload = _ban_file("25-Sep-2026", "1,sail", "2,M&M", "3,SAIL,")
        assert parse_fo_ban_list(payload) == (SESSION, ["M&M", "SAIL"])

    def test_a_file_without_the_header_is_refused(self) -> None:
        with pytest.raises(UnexpectedPayload, match="header"):
            parse_fo_ban_list(b"1,KAYNES\n2,SAIL\n")

    def test_an_unreadable_line_refuses_the_whole_list(self) -> None:
        """A missed symbol is a trade in a banned stock, so a partial read is never returned."""
        with pytest.raises(UnexpectedPayload, match="unreadable line"):
            parse_fo_ban_list(_ban_file("25-SEP-2026", "1,KAYNES", "<html>blocked</html>"))

    def test_an_empty_body_is_refused(self) -> None:
        with pytest.raises(UnexpectedPayload):
            parse_fo_ban_list(b"")
