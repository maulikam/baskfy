"""Append-only JSONL of every decision, fill and tick summary.

The book must be reconstructable after a crash from this file alone (operational rule R7),
which means nothing here rewrites or truncates. A journal that can be edited is not
evidence, and its whole purpose is to be the thing you believe when the code and your
memory disagree.

The order journal in app/core/gateway.py had 2,434 lines of test-written orders in it
before a conftest fixture stopped the suite reaching it. This one takes its path from
config so tests never touch the real file.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any, Iterator, Mapping


class Journal:
    def __init__(self, path: str) -> None:
        self.path = path
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    def write(self, event: str, **fields: Any) -> dict:
        rec = {"ts": dt.datetime.now().isoformat(timespec="seconds"), "event": event,
               **fields}
        with open(self.path, "a") as fh:
            fh.write(json.dumps(rec, default=str, separators=(",", ":")) + "\n")
        return rec

    def read(self) -> Iterator[dict]:
        if not os.path.exists(self.path):
            return
        with open(self.path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue

    def session(self, day: dt.date) -> list[dict]:
        want = day.isoformat()
        return [r for r in self.read() if str(r.get("ts", "")).startswith(want)]
