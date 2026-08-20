"""The raw-file archive (Prompt 2 deliverable 3).

docs/09 §"NSE specifics": "Fetch once, archive the raw file to R2 keyed by
`nse/{kind}/{date}.csv`, and parse from the archive. Never re-fetch to re-parse: the archive is
the reproducibility record."

Three properties follow from that sentence, and all three are asserted here:
the key layout, archive-before-parse ordering, and fetch-once.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from decile_providers.archive import (
    LocalRawArchive,
    S3RawArchive,
    archive_key,
    fetch_and_archive,
)
from decile_providers.errors import ArchiveError, CredentialsMissing

ON = dt.date(2026, 8, 18)


class TestKeyLayout:
    def test_the_key_matches_the_documented_layout(self) -> None:
        """docs/09: `nse/{kind}/{date}.csv`."""
        assert archive_key("bhavcopy", ON) == "nse/bhavcopy/2026-08-18.csv"

    def test_a_non_csv_extension_is_honoured(self) -> None:
        assert archive_key("bhavcopy", ON, "zip") == "nse/bhavcopy/2026-08-18.zip"

    def test_a_leading_dot_on_the_extension_is_tolerated(self) -> None:
        assert archive_key("listings", ON, ".json") == "nse/listings/2026-08-18.json"

    def test_an_empty_kind_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="kind"):
            archive_key("", ON)

    def test_the_date_is_sortable(self) -> None:
        """ISO dates sort lexically, so an operator listing the bucket sees chronological order."""
        keys = [archive_key("bhavcopy", dt.date(2026, 8, d)) for d in (18, 3, 11)]
        assert sorted(keys) == [
            "nse/bhavcopy/2026-08-03.csv",
            "nse/bhavcopy/2026-08-11.csv",
            "nse/bhavcopy/2026-08-18.csv",
        ]


class TestLocalArchive:
    def test_round_trip(self, tmp_path: Path) -> None:
        archive = LocalRawArchive(tmp_path)
        archive.put("nse/bhavcopy/2026-08-18.csv", b"SYMBOL,CLOSE\nSBIN,800\n")
        assert archive.get("nse/bhavcopy/2026-08-18.csv").startswith(b"SYMBOL")

    def test_exists_is_false_before_and_true_after(self, tmp_path: Path) -> None:
        archive = LocalRawArchive(tmp_path)
        key = archive_key("listings", ON)
        assert not archive.exists(key)
        archive.put(key, b"x")
        assert archive.exists(key)

    def test_reading_a_missing_key_raises_archive_error(self, tmp_path: Path) -> None:
        with pytest.raises(ArchiveError):
            LocalRawArchive(tmp_path).get("nse/bhavcopy/1999-01-01.csv")

    def test_a_key_cannot_escape_the_archive_root(self, tmp_path: Path) -> None:
        """Keys are built from provider-supplied names; they must not write outside the root."""
        archive = LocalRawArchive(tmp_path / "root")
        with pytest.raises(ArchiveError, match="escapes"):
            archive.put("../../etc/passwd", b"nope")


class FakeS3:
    def __init__(self, *, fail_on_put: bool = False) -> None:
        self.objects: dict[str, bytes] = {}
        self.metadata: dict[str, dict[str, str]] = {}
        self.fail_on_put = fail_on_put

    def put_object(self, **kwargs: object) -> object:
        if self.fail_on_put:
            raise RuntimeError("bucket is on fire")
        key = str(kwargs["Key"])
        body = kwargs["Body"]
        assert isinstance(body, bytes)
        self.objects[key] = body
        meta = kwargs.get("Metadata")
        if isinstance(meta, dict):
            self.metadata[key] = {str(k): str(v) for k, v in meta.items()}
        return {}

    def get_object(self, **kwargs: object) -> object:
        key = str(kwargs["Key"])
        if key not in self.objects:
            raise KeyError(key)
        return {"Body": _Body(self.objects[key])}

    def head_object(self, **kwargs: object) -> object:
        key = str(kwargs["Key"])
        if key not in self.objects:
            raise KeyError(key)
        return {}


class _Body:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload


class TestS3Archive:
    def test_round_trip(self) -> None:
        archive = S3RawArchive(FakeS3(), "decile-raw")
        uri = archive.put(archive_key("bhavcopy", ON), b"payload")
        assert uri == "s3://decile-raw/nse/bhavcopy/2026-08-18.csv"
        assert archive.get(archive_key("bhavcopy", ON)) == b"payload"

    def test_a_checksum_is_recorded(self) -> None:
        """Cheap corruption detection: what came back should be what went in."""
        client = FakeS3()
        archive = S3RawArchive(client, "decile-raw")
        key = archive_key("listings", ON)
        archive.put(key, b"payload")
        assert "sha256" in client.metadata[key]

    def test_a_failed_put_raises_archive_error(self) -> None:
        archive = S3RawArchive(FakeS3(fail_on_put=True), "decile-raw")
        with pytest.raises(ArchiveError, match="could not archive"):
            archive.put(archive_key("bhavcopy", ON), b"payload")

    def test_an_unconfigured_bucket_is_refused_at_construction(self) -> None:
        with pytest.raises(CredentialsMissing):
            S3RawArchive(FakeS3(), "")


class TestArchiveThenParse:
    """The ordering docs/09 mandates, and the reason it matters."""

    def test_the_file_is_archived_before_it_is_returned(self, tmp_path: Path) -> None:
        archive = LocalRawArchive(tmp_path)
        key = archive_key("bhavcopy", ON)
        payload = fetch_and_archive(archive, key, lambda: b"raw bytes")
        assert archive.exists(key)
        assert payload == b"raw bytes"

    def test_what_is_returned_is_read_back_from_the_archive(self, tmp_path: Path) -> None:
        """Not the bytes just fetched — otherwise "we saved a copy" and "we parsed the copy"
        can silently diverge, and a published number stops being reproducible."""
        archive = LocalRawArchive(tmp_path)
        key = archive_key("bhavcopy", ON)
        fetch_and_archive(archive, key, lambda: b"original")
        # Tamper with the archived copy; a re-read must observe the tampering.
        archive.put(key, b"tampered")
        assert fetch_and_archive(archive, key, lambda: b"original") == b"tampered"

    def test_an_already_archived_file_is_not_refetched(self, tmp_path: Path) -> None:
        """docs/09: "Never re-fetch to re-parse"."""
        archive = LocalRawArchive(tmp_path)
        key = archive_key("bhavcopy", ON)
        calls = 0

        def fetch() -> bytes:
            nonlocal calls
            calls += 1
            return b"payload"

        fetch_and_archive(archive, key, fetch)
        fetch_and_archive(archive, key, fetch)
        assert calls == 1

    def test_refetch_is_available_when_explicitly_asked_for(self, tmp_path: Path) -> None:
        archive = LocalRawArchive(tmp_path)
        key = archive_key("bhavcopy", ON)
        calls = 0

        def fetch() -> bytes:
            nonlocal calls
            calls += 1
            return b"payload"

        fetch_and_archive(archive, key, fetch)
        fetch_and_archive(archive, key, fetch, refetch=True)
        assert calls == 2

    def test_an_empty_file_is_refused(self, tmp_path: Path) -> None:
        """An empty upstream file is a publication failure, not a day with no trading."""
        archive = LocalRawArchive(tmp_path)
        with pytest.raises(ArchiveError, match="empty"):
            fetch_and_archive(archive, archive_key("bhavcopy", ON), lambda: b"")

    def test_a_failed_archive_aborts_the_parse(self) -> None:
        """The tempting shortcut — carry on with the bytes in memory — is the one docs/09
        forbids, because it produces a number nobody can re-derive."""
        archive = S3RawArchive(FakeS3(fail_on_put=True), "decile-raw")
        with pytest.raises(ArchiveError):
            fetch_and_archive(archive, archive_key("bhavcopy", ON), lambda: b"payload")
