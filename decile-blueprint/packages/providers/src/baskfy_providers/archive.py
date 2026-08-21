"""The raw-file archive (Prompt 2 deliverable 3).

docs/09 §"NSE specifics" is unambiguous about the ordering:

    "Fetch once, archive the raw file to R2 keyed by `nse/{kind}/{date}.csv`, and parse from the
    archive. Never re-fetch to re-parse: the archive is the reproducibility record."

So the contract here is *archive-then-parse*, and a failed archive must abort the parse. It is
tempting to treat archiving as best-effort telemetry and carry on with the bytes already in
memory — that is exactly the shortcut that produces a published number nobody can re-derive six
months later when an exchange file turns out to have been wrong.

Two implementations, one interface:

* :class:`S3RawArchive`   — Cloudflare R2 or any S3-compatible store (docs/02).
* :class:`LocalRawArchive` — a directory on disk, for local development and tests, so the
  archive-then-parse path is exercised everywhere rather than bypassed when S3 is absent.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from baskfy_providers.errors import ArchiveError, CredentialsMissing


def archive_key(kind: str, on: dt.date, extension: str = "csv") -> str:
    """``nse/{kind}/{date}.{ext}`` — the layout docs/09 specifies."""
    if not kind:
        raise ValueError("archive kind cannot be empty")
    cleaned = extension.lstrip(".")
    return f"nse/{kind}/{on.isoformat()}.{cleaned}"


class RawArchive(Protocol):
    """Where raw upstream files are preserved before anything parses them."""

    def put(self, key: str, payload: bytes, *, content_type: str = "text/csv") -> str:
        """Store ``payload`` at ``key``. Returns a URI for logs. Raises ArchiveError on failure."""
        ...

    def get(self, key: str) -> bytes:
        """Read back what was stored. This is what parsers read from."""
        ...

    def exists(self, key: str) -> bool: ...

    def describe(self) -> str:
        """Human-readable location, for `providers doctor`."""
        ...


class LocalRawArchive:
    """A directory on disk. Same contract as S3, no credentials."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    def _path(self, key: str) -> Path:
        candidate = (self._root / key).resolve()
        root = self._root.resolve()
        # A key is built from provider-supplied names; keep it from escaping the archive root.
        if not candidate.is_relative_to(root):
            raise ArchiveError(f"archive key {key!r} escapes the archive root")
        return candidate

    def put(self, key: str, payload: bytes, *, content_type: str = "text/csv") -> str:
        del content_type  # a filesystem has nowhere to record it
        path = self._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        except OSError as exc:
            raise ArchiveError(f"could not archive {key!r} to {path}: {exc}") from exc
        return path.as_uri()

    def get(self, key: str) -> bytes:
        path = self._path(key)
        try:
            return path.read_bytes()
        except OSError as exc:
            raise ArchiveError(f"could not read {key!r} from {path}: {exc}") from exc

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def describe(self) -> str:
        return f"local directory {self._root}"


class S3ClientLike(Protocol):
    """The slice of boto3's S3 client this module uses."""

    def put_object(self, **kwargs: object) -> object: ...

    def get_object(self, **kwargs: object) -> object: ...

    def head_object(self, **kwargs: object) -> object: ...


class S3RawArchive:
    """Cloudflare R2, or any S3-compatible bucket (docs/02 §"Object storage")."""

    def __init__(self, client: S3ClientLike, bucket: str) -> None:
        if not bucket:
            raise CredentialsMissing("no S3 bucket configured for the raw archive")
        self._client = client
        self._bucket = bucket

    def put(self, key: str, payload: bytes, *, content_type: str = "text/csv") -> str:
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=payload,
                ContentType=content_type,
                # Cheap tamper/corruption detection on the way back out.
                Metadata={"sha256": hashlib.sha256(payload).hexdigest()},
            )
        except Exception as exc:
            raise ArchiveError(f"could not archive {key!r} to s3://{self._bucket}: {exc}") from exc
        return f"s3://{self._bucket}/{key}"

    def get(self, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except Exception as exc:
            raise ArchiveError(f"could not read {key!r} from s3://{self._bucket}: {exc}") from exc
        return _read_body(response, key)

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
        except Exception:
            return False
        return True

    def describe(self) -> str:
        return f"s3://{self._bucket}"


def _read_body(response: object, key: str) -> bytes:
    if not isinstance(response, dict) or "Body" not in response:
        raise ArchiveError(f"unexpected S3 response for {key!r}")
    body = response["Body"]
    read = getattr(body, "read", None)
    if not callable(read):
        raise ArchiveError(f"unexpected S3 body for {key!r}")
    payload = read()
    if not isinstance(payload, bytes):
        raise ArchiveError(f"S3 returned a non-bytes body for {key!r}")
    return payload


def fetch_and_archive(
    archive: RawArchive,
    key: str,
    fetch: Callable[[], bytes],
    *,
    content_type: str = "text/csv",
    refetch: bool = False,
) -> bytes:
    """The archive-then-parse discipline, in one place.

    Returns the bytes **read back from the archive**, never the bytes just fetched. That is the
    difference between "we saved a copy" and "what we parsed is what we saved": if the archive
    silently truncated or transcoded, the parse fails now instead of producing a number that
    cannot be reproduced from the record.

    An already-archived key is not re-fetched, which is docs/09's "Never re-fetch to re-parse".
    """
    if refetch or not archive.exists(key):
        payload = fetch()
        if not payload:
            raise ArchiveError(f"fetcher for {key!r} returned an empty file")
        archive.put(key, payload, content_type=content_type)
    return archive.get(key)
