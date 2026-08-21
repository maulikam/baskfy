#!/usr/bin/env python
"""Back up the irreplaceable half of this system.

WHAT CANNOT BE REBUILT, and is therefore what this exists for:
    snapshots         the entire performance record. kc.margins() has no history, so a
                      lost EOD row is a lost day, permanently.
    fills             every execution. Zerodha flushes /trades nightly; the only other
                      copy is a manual Console export covering part of the history.
    trades            the realised record, derived from fills.
    breadth_readings  cannot be rebuilt from a scan you no longer have.
    rebalance_orders  what each plan intended and what actually happened.
    the journals       the audit record of every order the gateway ever touched.

index_series and benchmark ARE re-fetchable from Kite, so nothing here depends on them —
but they cost almost nothing to carry, and a restore that needs a second fetch is a
restore that stalls.

    python -m scripts.backup                  write one to data/backups
    python -m scripts.backup --check          report what exists, restore nothing
    python -m scripts.backup --to /mnt/x      somewhere else, e.g. a mounted volume

A LIVE SQLITE FILE MUST NOT BE COPIED. A plain cp can capture a write in progress and
produce an archive that opens fine and is missing the last transaction — the worst kind of
backup, because it looks like one. sqlite3's own backup API takes a consistent snapshot
while the database is in use, and that is what this uses.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sqlite3
import sys
import tarfile

DB = pathlib.Path("data/portfolio.db")
OUTPUTS = pathlib.Path("data/outputs")
DEST = pathlib.Path("data/backups")
KEEP = 30                       # days; the whole set is ~10 MB, so this is not the cost


def consistent_copy(src: pathlib.Path, dst: pathlib.Path) -> int:
    """A snapshot taken through sqlite's backup API, safe while the desk is writing."""
    with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as source, \
            sqlite3.connect(dst) as target:
        source.backup(target)
    return dst.stat().st_size


def verify(path: pathlib.Path) -> dict:
    """Open it and count the rows that matter. A backup nobody has read is a hope."""
    out: dict = {}
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as c:
        c.row_factory = sqlite3.Row
        ok = c.execute("PRAGMA integrity_check").fetchone()[0]
        out["integrity"] = ok
        for t in ("snapshots", "fills", "trades", "rebalance_orders",
                  "breadth_readings", "regime_evaluations"):
            try:
                out[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.Error:
                out[t] = None
    return out


def run(dest_root: pathlib.Path) -> dict:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = dest_root / stamp
    dest.mkdir(parents=True, exist_ok=True)

    db_copy = dest / "portfolio.db"
    size = consistent_copy(DB, db_copy) if DB.exists() else 0
    checks = verify(db_copy) if size else {}

    tar_path = dest / "outputs.tar.gz"
    n_files = 0
    if OUTPUTS.exists():
        with tarfile.open(tar_path, "w:gz") as tar:
            for f in sorted(OUTPUTS.rglob("*")):
                if f.is_file():
                    tar.add(f, arcname=str(f.relative_to(OUTPUTS)))
                    n_files += 1

    report = {"at": dt.datetime.now().isoformat(timespec="seconds"),
              "dir": str(dest), "db_bytes": size, "output_files": n_files,
              "verified": checks}
    (dest / "manifest.json").write_text(json.dumps(report, indent=2))
    return report


def prune(dest_root: pathlib.Path, keep_days: int = KEEP) -> list[str]:
    cutoff = dt.datetime.now() - dt.timedelta(days=keep_days)
    gone = []
    for d in sorted(dest_root.glob("20*-*")):
        try:
            when = dt.datetime.strptime(d.name, "%Y%m%d-%H%M%S")
        except ValueError:
            continue
        if when < cutoff:
            for f in sorted(d.rglob("*"), reverse=True):
                f.unlink() if f.is_file() else f.rmdir()
            d.rmdir()
            gone.append(d.name)
    return gone


def main() -> int:
    ap = argparse.ArgumentParser(description="back up what cannot be rebuilt")
    ap.add_argument("--to", default=str(DEST))
    ap.add_argument("--check", action="store_true", help="report only")
    ap.add_argument("--keep", type=int, default=KEEP)
    a = ap.parse_args()
    root = pathlib.Path(a.to)

    if a.check:
        existing = sorted(root.glob("20*-*"))
        latest = existing[-1] if existing else None
        print(json.dumps({
            "backups": len(existing),
            "latest": latest.name if latest else None,
            "latest_manifest": (json.loads((latest / "manifest.json").read_text())
                                if latest and (latest / "manifest.json").exists() else None),
        }, indent=2))
        return 0

    report = run(root)
    report["pruned"] = prune(root, a.keep)
    print(json.dumps(report, indent=2))
    # An archive whose integrity check does not say "ok" is not a backup.
    return 0 if report["verified"].get("integrity") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
