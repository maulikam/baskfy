"""Do both backends render the same numbers, or merely both return 200? — M19 §3's acceptance.

    .venv/bin/python -m scripts.backend_parity

Renders every one of the desk's HTML pages twice, once on SQLite and once on Postgres, in two
separate subprocesses so the backend is chosen at import time exactly as it would be in production.
Then it compares the HTML.

WHY BYTES AND NOT STATUS CODES
-------------------------------
"All 13 pages work" is satisfied by thirteen 200s over an empty database. The question that matters
is whether the desk shows the *same numbers* on the merged backend, and only a content comparison
answers it. Two of the four defects this found returned 200 on both sides:

* `/tradebook` raised on `float / Decimal`, because Postgres returns NUMERIC as `Decimal` and nine
  and a half thousand lines of desk analytics assume float;
* `/regime` used SQLite's bare-column extension — `SELECT col, MAX(other) ... GROUP BY` — which
  Postgres refuses outright and any other engine may answer from an arbitrary row;
* `/regime`'s diagnostics panel wrapped five probes in one `try/except: pass`, so the first PRAGMA
  to fail blanked the other four;
* and the plan panel differed because the Postgres copy was **stale** — which is not a code defect
  at all, but the divergence window M18.2 predicted, caught by exactly this comparison.

WHAT IS EXPECTED TO DIFFER
---------------------------
The journal mode and the on-disk size. Those two lines describe the storage engine rather than the
desk's data, and they *should* differ — a run where they matched would mean the backend switch had
not taken effect.
"""

from __future__ import annotations

import json
import re
import subprocess

#: Every HTML route except the parameterised ones and /options, which 404s on both backends
#: because the strangle subsystem is frozen (M6).
PAGES = [
    "/",
    "/reconcile",
    "/stops",
    "/indices",
    "/regime",
    "/regime/backtest",
    "/ops",
    "/performance",
    "/tradebook",
    "/settings",
    "/status",
]


def bodies(backend: str) -> dict[str, str]:
    """Render every page in a fresh interpreter with the backend chosen at import time.

    A subprocess rather than a monkeypatch: `app.analytics.db` reads DESK_DB_BACKEND once, at
    import, exactly as the running desk does. Flipping it inside a live process would test a
    configuration that never exists in production.
    """
    code = f"""
import os, sys, json
os.environ["DESK_DB_BACKEND"] = "{backend}"
sys.path.insert(0, ".")
from fastapi.testclient import TestClient
from app import main as M
c = TestClient(M.app)
print("@@@" + json.dumps({{p: c.get(p).text for p in {PAGES!r}}}))
"""
    result = subprocess.run(
        [".venv/bin/python", "-c", code], capture_output=True, text=True, check=False
    )
    marked = [line for line in result.stdout.splitlines() if line.startswith("@@@")]
    if not marked:
        print(result.stderr[-1200:])
        raise SystemExit(f"could not render pages on {backend}")
    return json.loads(marked[0][3:])


def normalise(html: str) -> str:
    """Remove what legitimately differs between two runs a second apart."""
    html = re.sub(r"\b\d{10}\.\d+\b", "TS", html)  # epoch timestamps
    html = re.sub(r"\?v=[0-9a-f]+", "?v=V", html)  # asset cache-buster
    return re.sub(r"\b\d{4}-\d{2}-\d{2}T[\d:.]+\b", "ISO", html)  # generated-at stamps


def main() -> int:
    left, right = bodies("sqlite"), bodies("postgres")
    same = differing = 0
    for page in PAGES:
        a, b = normalise(left[page]), normalise(right[page])
        if a == b:
            same += 1
            print(f"  identical  {page}")
            continue
        differing += 1
        la, lb = a.splitlines(), b.splitlines()
        print(f"  DIFFERS    {page}   ({len(la)} vs {len(lb)} lines)")
        for i, (x, y) in enumerate(zip(la, lb, strict=False)):
            if x != y:
                print(f"      line {i}: sqlite   {x.strip()[:110]}")
                print(f"      line {i}: postgres {y.strip()[:110]}")
    print(f"\n{same} identical, {differing} differing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
