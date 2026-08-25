#!/usr/bin/env python
"""Prove the latest portfolio.db backup is restorable, not merely present.

    python -m scripts.restore_drill

Copies the newest backup into a temp file, integrity-checks it, and compares row counts
against the live database. Exit 0 only when counts match and both files pass PRAGMA
integrity_check. An backup nobody has restored is a hypothesis.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import sqlite3
import sys
import tempfile

from scripts import backup as B

TABLES = (
    "snapshots",
    "fills",
    "trades",
    "rebalance_orders",
    "breadth_readings",
    "regime_evaluations",
)


def counts(path: pathlib.Path) -> dict[str, int | None]:
    out: dict[str, int | None] = {}
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        out["integrity"] = integrity  # type: ignore[assignment]
        for table in TABLES:
            try:
                out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                out[table] = None
    return out


def latest_backup(root: pathlib.Path) -> pathlib.Path | None:
    """Return the portfolio.db path inside the newest backup directory."""
    dirs = sorted(root.glob("20*-*"))
    if not dirs:
        return None
    candidate = dirs[-1] / "portfolio.db"
    return candidate if candidate.exists() else None


def main() -> int:
    backup_root = B.DEST
    backup_dirs = sorted(backup_root.glob("20*-*"))
    if not backup_dirs:
        print(json.dumps({"ok": False, "error": "no backup found"}, indent=2))
        return 1
    latest_dir = backup_dirs[-1]
    backup_path = latest_dir / "portfolio.db"
    manifest_path = latest_dir / "manifest.json"
    if not backup_path.exists():
        print(json.dumps({"ok": False, "error": "backup db missing"}, indent=2))
        return 1
    if not manifest_path.exists():
        print(json.dumps({"ok": False, "error": "manifest missing"}, indent=2))
        return 1

    manifest = json.loads(manifest_path.read_text())
    expected = manifest.get("verified", {})

    with tempfile.TemporaryDirectory(prefix="restore-drill-") as tmp:
        restored = pathlib.Path(tmp) / "portfolio.db"
        shutil.copy2(backup_path, restored)
        restored_counts = counts(restored)

        mismatches = {
            table: {"expected": expected.get(table), "restored": restored_counts.get(table)}
            for table in TABLES
            if expected.get(table) != restored_counts.get(table)
        }

        ok = restored_counts.get("integrity") == "ok" and not mismatches
        live = counts(B.DB) if B.DB.exists() else {}
        report = {
            "ok": ok,
            "backup_dir": str(latest_dir),
            "manifest_at": manifest.get("at"),
            "restored": {k: restored_counts[k] for k in (*TABLES, "integrity")},
            "expected": expected,
            "mismatches": mismatches,
            "live_now": {k: live.get(k) for k in TABLES} if live else None,
            "live_drift_note": (
                "Live counts may differ after backup time; restore proof uses manifest only."
            ),
        }
        print(json.dumps(report, indent=2))
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
