"""Backing up what cannot be rebuilt.

The whole irreplaceable set is about 10 MB — but `snapshots` IS the performance record and
kc.margins() has no history, `fills` is the execution record and Zerodha flushes /trades
nightly, and `breadth_readings` cannot be reconstructed from a scan you no longer have.
Losing the disk would not cost storage, it would cost the evidence the strategy is being
judged on.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import threading

import pytest

from scripts import backup as B


@pytest.fixture()
def live_db(tmp_path, monkeypatch):
    """A database that is being written to, because that is when a copy goes wrong."""
    db = tmp_path / "portfolio.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE snapshots (date TEXT, nav REAL)")
    c.execute("CREATE TABLE fills (trade_id TEXT)")
    c.executemany("INSERT INTO snapshots VALUES (?,?)",
                  [(f"2026-08-{d:02d}", 1000.0 + d) for d in range(1, 11)])
    c.commit()
    monkeypatch.setattr(B, "DB", db)
    monkeypatch.setattr(B, "OUTPUTS", tmp_path / "outputs")
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "journal.jsonl").write_text('{"event":"placed"}\n')
    return db, c


def test_a_backup_is_verified_not_merely_written(live_db, tmp_path):
    """An archive nobody has opened is a hope. Every run integrity-checks the copy and
    counts the rows that matter, and the exit code follows that check."""
    r = B.run(tmp_path / "b")
    assert r["verified"]["integrity"] == "ok"
    assert r["verified"]["snapshots"] == 10
    assert r["output_files"] == 1


def test_the_copy_is_consistent_while_the_database_is_being_written(live_db, tmp_path):
    """A plain cp can capture a write in progress and produce a file that opens fine and
    is missing the last transaction — the worst kind of backup, because it looks like one.
    sqlite's own backup API takes a consistent snapshot of a live database."""
    db, conn = live_db
    stop = threading.Event()

    def writer():
        w = sqlite3.connect(db)
        n = 0
        while not stop.is_set() and n < 400:
            w.execute("INSERT INTO fills VALUES (?)", (str(n),))
            w.commit()
            n += 1
        w.close()

    t = threading.Thread(target=writer)
    t.start()
    try:
        r = B.run(tmp_path / "b")
    finally:
        stop.set()
        t.join()
    assert r["verified"]["integrity"] == "ok"


def test_it_uses_sqlites_backup_api_rather_than_copying_the_file():
    import inspect
    src = inspect.getsource(B.consistent_copy)
    assert ".backup(" in src
    assert "shutil.copy" not in src and "copyfile" not in src


def test_a_manifest_records_what_was_taken(live_db, tmp_path):
    r = B.run(tmp_path / "b")
    manifest = json.loads((tmp_path / "b" / r["dir"].split("/")[-1] / "manifest.json").read_text())
    assert manifest["verified"]["snapshots"] == 10


def test_old_backups_are_pruned_but_recent_ones_are_not(live_db, tmp_path):
    root = tmp_path / "b"
    B.run(root)
    assert B.prune(root, keep_days=30) == []          # today's survives
    assert len(list(root.glob("20*-*"))) == 1


def test_a_corrupt_copy_makes_the_run_fail(live_db, tmp_path, monkeypatch):
    """So a broken backup is loud in `systemctl status`, not silently green."""
    monkeypatch.setattr(B, "verify", lambda p: {"integrity": "malformed"})
    monkeypatch.setattr(sys, "argv", ["backup", "--to", str(tmp_path / "b")])
    assert B.main() == 1


def test_the_timer_runs_after_the_daily_collection():
    """Backing up at 18:00 would archive yesterday's EOD snapshot and miss the one the
    daily job writes at 18:30."""
    import pathlib
    t = pathlib.Path("deploy/systemd/momentum-backup.timer").read_text()
    assert "19:15" in t and "Persistent=true" in t


def test_the_desk_is_capped_so_the_cli_is_killed_first():
    """The Claude CLI shares the box and is the memory-hungry half — 467 MB measured
    against the desk's 148 MB. Under pressure the kernel should take the interactive tool,
    not the process holding live plan_ids."""
    import pathlib
    unit = pathlib.Path("deploy/systemd/momentum-web.service").read_text()
    assert "MemoryMax=" in unit and "MemoryHigh=" in unit


def test_restore_drill_matches_the_latest_manifest(live_db, tmp_path):
    """A backup nobody has restored is a hypothesis — copy it out and read it."""
    root = tmp_path / "backups"
    B.run(root)
    import scripts.restore_drill as drill

    drill.B.DEST = root
    drill.B.DB = live_db[0]
    assert drill.main() == 0
