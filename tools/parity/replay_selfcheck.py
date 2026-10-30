"""Prove the two replay adapters answer identically (DECISIONS-RS R7.3).

    cd /Volumes/T7-Work/Dev/projects/baskfy/decile-blueprint
    PYTHONPATH=..:packages/core/src:packages/providers/src:services/api/src:services/worker/src \\
        .venv/bin/python ../tools/parity/replay_selfcheck.py rust/target/replay/2026-10-29/base

Runs a fixed list of calls — every method, answered and refused, plus the edges (a token with no
bar that day, an unknown token, a window outside the bundle, a path-traversal slug, duplicate and
unknown symbols) — through ``replay_provider.ReplayProvider`` here and through the Rust adapter
(``cargo run -p baskfy-providers --example replay_probe``), and requires the two outputs to be
equal value for value: records as JSON, frames as their schema and every cell's string form, and
a refusal as its Python class and ``str()``. Exits non-zero on any difference.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
from replay_provider import ReplayProvider  # noqa: E402

from baskfy_providers.errors import ProviderError  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
CARGO = Path.home() / ".cargo" / "bin" / "cargo"


def kind(dtype: pl.DataType) -> str:
    if dtype == pl.String:
        return "str"
    if dtype == pl.Date:
        return "date"
    if dtype == pl.Int64:
        return "i64"
    if isinstance(dtype, pl.Decimal):
        return f"dec({dtype.precision},{dtype.scale})"
    return str(dtype)


def cell(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, (int, Decimal)):
        return str(value)
    if isinstance(value, dt.date):
        return value.isoformat()
    return value


def frame(df: pl.DataFrame) -> dict[str, Any]:
    return {
        "schema": [[name, kind(dtype)] for name, dtype in df.schema.items()],
        "rows": [[cell(v) for v in row] for row in df.iter_rows()],
    }


def answer(call: dict[str, Any], p: ReplayProvider) -> dict[str, Any]:
    def d(key: str) -> dt.date:
        return dt.date.fromisoformat(call[key])

    try:
        m = call["method"]
        if m in ("daily_bars", "bhavcopy"):
            df = p.daily_bars(call["token"], d("start"), d("end")) if m == "daily_bars" else p.bhavcopy(d("on"))
            return {"ok": frame(df)}
        result: Any
        if m == "index_level":
            result = p.index_level(call["slug"])
        elif m == "corporate_actions":
            result = p.corporate_actions(d("since"))
        elif m == "index_constituents":
            result = p.index_constituents(call["index_slug"], d("on"))
        elif m == "index_snapshots":
            result = p.index_snapshots(d("on"))
        elif m == "equity_fundamentals":
            result = p.equity_fundamentals(d("on"), call["symbols"], series_by_symbol={})
        else:
            result = getattr(p, m)()
    except ProviderError as exc:
        return {"error_type": type(exc).__name__, "error": str(exc)}
    if isinstance(result, list):
        result = [r.model_dump(mode="json") if hasattr(r, "model_dump") else r for r in result]
    return {"ok": result}


def calls_for(bundle: Path) -> list[dict[str, Any]]:
    manifest = json.loads((bundle / "manifest.json").read_text())
    day = manifest["trade_date"]
    bars = json.loads((bundle / "calls" / "daily_bars.json").read_text())["by_token"]
    with_bar = [t for t, rows in bars.items() if rows][:3]
    without = [t for t, rows in bars.items() if not rows][:1]
    funds = json.loads((bundle / "calls" / "equity_fundamentals" / f"{day}.json").read_text())
    syms = [r["symbol"] for r in funds[:3]]
    since = (dt.date.fromisoformat(day) - dt.timedelta(days=7)).isoformat()
    calls: list[dict[str, Any]] = [
        {"method": "list_instruments"},
        {"method": "listings"},
        {"method": "sme_listings"},
        {"method": "etf_symbols"},
        {"method": "fno_underlyings"},
        {"method": "index_level", "slug": "nifty-50"},
        {"method": "bhavcopy", "on": day},
        {"method": "bhavcopy", "on": "2026-10-28"},
        {"method": "corporate_actions", "since": since},
        {"method": "corporate_actions", "since": day},
        {"method": "index_constituents", "index_slug": "nifty-50", "on": day},
        {"method": "index_constituents", "index_slug": "nifty-fno", "on": day},
        {"method": "index_constituents", "index_slug": "../manifest", "on": day},
        {"method": "index_snapshots", "on": day},
        {"method": "index_snapshots", "on": "2026-10-30"},
        {"method": "equity_fundamentals", "on": day, "symbols": [*syms, syms[0], "NOSUCHSYMBOL"]},
        {"method": "daily_bars", "token": 999_999_999_999, "start": day, "end": day},
        {"method": "daily_bars", "token": int(with_bar[0]), "start": "2026-10-28", "end": day},
    ]
    calls += [{"method": "daily_bars", "token": int(t), "start": day, "end": day} for t in with_bar + without]
    return calls


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    bundle = Path(argv[0]).resolve()
    calls = calls_for(bundle)
    python = [answer(c, ReplayProvider(bundle)) for c in calls]
    with tempfile.TemporaryDirectory() as tmp:
        calls_file = Path(tmp) / "calls.json"
        calls_file.write_text(json.dumps(calls))
        env = {**os.environ, "CARGO_TARGET_DIR": str(ROOT / "rust" / "target"), "CARGO_BUILD_JOBS": "6"}
        out = subprocess.run(
            ["nice", "-n", "10", str(CARGO), "run", "-q", "-p", "baskfy-providers", "--example",
             "replay_probe", "--", str(bundle), str(calls_file)],
            cwd=ROOT / "rust", env=env, capture_output=True, text=True, check=False,
        )
    if out.returncode != 0:
        print(out.stderr)
        return 1
    rust = json.loads(out.stdout)
    differences = 0
    for call, py, rs in zip(calls, python, rust, strict=True):
        label = json.dumps(call)[:100]
        if py == rs:
            print(f"EQUAL  {label}")
        else:
            differences += 1
            print(f"DIFFER {label}\n  python: {json.dumps(py)[:400]}\n  rust:   {json.dumps(rs)[:400]}")
    print(f"{len(calls) - differences} of {len(calls)} calls EQUAL over {bundle}")
    return 1 if differences else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
