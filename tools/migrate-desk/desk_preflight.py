"""The safety rail, enforced in code rather than in a checklist.

Root `CLAUDE.md`: *"`kite-momentum-rebalancer/data/portfolio.db` is unrebuildable evidence …
Before any step that can touch it: a verified backup (`python -m scripts.backup` must say `ok`)
plus a dated copy outside the repo."* And D8: *"the file is archived forever."*

A rail that lives only in a document is a rail somebody steps over at 23:40 on a Sunday. So this
module is what `migrate_desk.py apply` calls before it opens a write connection, and every one of
these refusals aborts the run:

1. **A verified backup manifest.** `scripts/backup.py` writes `manifest.json` next to the copy it
   took through SQLite's backup API, and only calls the run a success when
   `verified.integrity == "ok"`. This re-reads that file and re-checks the claim; a manifest whose
   integrity is anything but `ok` is not a backup.
2. **A dated copy outside the repo, that opens.** Not "a path that exists" — the file is opened
   read-only and `PRAGMA quick_check` must say `ok`, and its sha256 goes into the report. An
   archive nobody has read is a hope.
3. **The archive is outside the repository.** A backup inside the working tree dies with the
   working tree.
4. **The source is unchanged.** Its sha256 is taken before the run and again after, and the run
   fails if they differ — the source is opened read-only, so a difference means something *else*
   wrote to it while the migration was reading, and the copy is of a moving target.
5. **A rehearsal happened, against this exact source.** `apply --require-rehearsal` looks for a
   receipt whose `source_sha256` matches, and refuses without one. Rehearsing against a copy is a
   contract term, not advice.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import sqlite3
from dataclasses import dataclass
from typing import Any


class PreflightRefusal(RuntimeError):
    """A safety rail said no. Nothing has been opened for writing."""


CHUNK = 1 << 20


def sha256_of(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class Fingerprint:
    path: str
    sha256: str
    bytes: int
    mtime: str

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256, "bytes": self.bytes,
                "mtime": self.mtime}


def fingerprint(path: str | os.PathLike[str]) -> Fingerprint:
    p = pathlib.Path(path)
    stat = p.stat()
    return Fingerprint(
        path=str(p),
        sha256=sha256_of(p),
        bytes=stat.st_size,
        mtime=dt.datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="microseconds"),
    )


def quick_check(path: str | os.PathLike[str]) -> str:
    conn = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
    try:
        return str(conn.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        conn.close()


def verify_backup_manifest(manifest_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Rail 1: `python -m scripts.backup` said ok, and here is the file where it said so."""
    p = pathlib.Path(manifest_path)
    if not p.is_file():
        raise PreflightRefusal(
            f"no backup manifest at {p}. Run `python -m scripts.backup` against the source and "
            "point --backup-manifest at the manifest.json it wrote."
        )
    try:
        manifest = json.loads(p.read_text())
    except json.JSONDecodeError as exc:
        raise PreflightRefusal(f"backup manifest at {p} is not JSON: {exc}") from exc
    integrity = (manifest.get("verified") or {}).get("integrity")
    if integrity != "ok":
        raise PreflightRefusal(
            f"backup manifest at {p} reports integrity {integrity!r}, not 'ok'. "
            "That is not a backup and the migration will not run against it."
        )
    return manifest


def verify_archive(
    archive_path: str | os.PathLike[str], repo_root: str | os.PathLike[str] | None = None
) -> dict[str, Any]:
    """Rails 2 and 3: the dated copy exists outside the repo, opens, and passes quick_check."""
    p = pathlib.Path(archive_path).expanduser().resolve()
    if not p.is_file():
        raise PreflightRefusal(f"no archive copy at {p}")
    if repo_root is not None:
        root = pathlib.Path(repo_root).expanduser().resolve()
        if root == p or root in p.parents:
            raise PreflightRefusal(
                f"the archive copy {p} is inside the repository at {root}. A backup that lives "
                "in the working tree dies with the working tree; put it somewhere else."
            )
    check = quick_check(p)
    if check != "ok":
        raise PreflightRefusal(f"archive copy {p} fails quick_check: {check!r}")
    fp = fingerprint(p)
    writable = os.access(p, os.W_OK)
    return {
        "path": str(p),
        "sha256": fp.sha256,
        "bytes": fp.bytes,
        "quick_check": check,
        "read_only_on_disk": not writable,
        "outside_repo": True,
    }


# ---------------------------------------------------------------- rehearsal receipts


def receipt_path(
    receipts_dir: str | os.PathLike[str], source_sha256: str, fork_policy: str
) -> pathlib.Path:
    """Keyed on the source's sha256 AND the fork policy.

    Both, because both are what the rehearsal proves. A receipt keyed on the file alone would let
    a rehearsal of `box-only` wave through an apply of `box-plus-orphans`, which lands 76 extra
    rows the rehearsal never inserted.
    """
    return (
        pathlib.Path(receipts_dir).expanduser()
        / f"rehearsal-{source_sha256[:16]}-{fork_policy}.json"
    )


def write_receipt(
    receipts_dir: str | os.PathLike[str], payload: dict[str, Any]
) -> pathlib.Path:
    directory = pathlib.Path(receipts_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    path = receipt_path(directory, payload["source"]["sha256"], payload["fork_policy"])
    path.write_text(json.dumps(payload, indent=2, default=str))
    return path


def require_rehearsal(
    receipts_dir: str | os.PathLike[str], source_sha256: str, fork_policy: str
) -> dict[str, Any]:
    """Rail 5. The rehearsal must have run against this byte-identical source.

    Keying on the sha256 rather than on the path is the whole point: a rehearsal against
    yesterday's copy proves nothing about today's file, and a path can be re-pointed.
    """
    path = receipt_path(receipts_dir, source_sha256, fork_policy)
    if not path.is_file():
        raise PreflightRefusal(
            f"no rehearsal receipt for source sha256 {source_sha256[:16]}… under fork policy "
            f"{fork_policy!r} in {receipts_dir}.\n"
            "The contract is that the migration is rehearsed against a copy before it touches a "
            "real target. Run `migrate_desk.py rehearse` against this exact file first."
        )
    receipt = json.loads(path.read_text())
    if not receipt.get("ok"):
        raise PreflightRefusal(f"the rehearsal recorded at {path} did not pass; apply refused")
    if receipt.get("fork_policy") != fork_policy:
        raise PreflightRefusal(
            f"the rehearsal at {path} ran under fork policy {receipt.get('fork_policy')!r} but "
            f"this run asks for {fork_policy!r}. Rehearse the policy you intend to apply — the "
            "policies differ in what rows land, which is the whole thing being rehearsed."
        )
    return receipt


def assert_source_unmoved(before: Fingerprint, path: str | os.PathLike[str]) -> Fingerprint:
    """Rail 4. Prove the source did not change under us, and was never written to."""
    after = fingerprint(path)
    if before.sha256 != after.sha256 or before.bytes != after.bytes:
        raise PreflightRefusal(
            "the source database changed while the migration was reading it:\n"
            f"  before  {before.sha256}  {before.bytes} bytes  {before.mtime}\n"
            f"  after   {after.sha256}  {after.bytes} bytes  {after.mtime}\n"
            "This tool opens the source read-only, so something else wrote to it. The copy is of "
            "a moving target and must not be trusted."
        )
    return after
