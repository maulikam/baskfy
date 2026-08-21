"""Exercise every NSE provider capability against the live site (MERGE-PROMPTS.md M8).

`decile-blueprint` was built with the test suite network-blocked, so every NSE URL shape,
header and column name in `baskfy_providers.nse` was written from documented file layouts and
had **never been fetched**. Its own CLAUDE.md says so: "NSE URL shapes and column names are
still unverified. Confirm each URL and header row against a real fetch before the first
production backfill."

This is that fetch. It goes through `NSEProvider` itself — the same cookie priming, the same
token-bucket throttle, the same retry and circuit breaker — because the safety rails forbid
scraping around the rate-limited providers, and because a check that bypasses the client under
test proves nothing about the client.

    uv run python ../tools/verify-nse.py [--date YYYY-MM-DD]

Writes a JSON report to stdout. Every fetch is archived by the provider's own archive path.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import traceback
from typing import Any


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-08-21", help="a trading day to ask for")
    args = ap.parse_args()
    on = dt.date.fromisoformat(args.date)

    from baskfy_providers.factory import build_nse_provider
    from baskfy_providers.settings import ProviderSettings

    settings = ProviderSettings()
    nse = build_nse_provider(settings)
    print(f"archive: {settings.raw_archive_dir if hasattr(settings, 'raw_archive_dir') else '(default)'}",
          file=sys.stderr)

    results: list[dict[str, Any]] = []

    def probe(name: str, url_shape: str, fn) -> None:
        entry: dict[str, Any] = {"capability": name, "url_shape": url_shape}
        try:
            value = fn()
            entry["ok"] = True
            if hasattr(value, "height"):                      # a polars frame
                entry["rows"] = int(value.height)
                entry["columns"] = list(value.columns)[:12]
                entry["column_count"] = len(value.columns)
            elif isinstance(value, list):
                entry["rows"] = len(value)
                entry["sample"] = _describe(value[0]) if value else None
            else:
                entry["value"] = repr(value)[:200]
        except Exception as exc:                              # noqa: BLE001 — this is the report
            entry["ok"] = False
            entry["error_type"] = type(exc).__name__
            entry["error"] = str(exc)[:400]
            entry["where"] = traceback.format_exc().strip().splitlines()[-3:]
        results.append(entry)
        flag = "ok  " if entry.get("ok") else "FAIL"
        detail = entry.get("rows", entry.get("error_type", ""))
        print(f"[{flag}] {name:22} {detail}", file=sys.stderr)

    def _describe(item: Any) -> Any:
        if hasattr(item, "__dict__"):
            return {k: str(v)[:60] for k, v in list(vars(item).items())[:10]}
        if hasattr(item, "_asdict"):
            return {k: str(v)[:60] for k, v in item._asdict().items()}
        return str(item)[:200]

    probe("listings", "{archive}/content/equities/EQUITY_L.csv", nse.listings)
    probe("bhavcopy", "{archive}/content/cm/BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip",
          lambda: nse.bhavcopy(on))
    probe("index_snapshots", "{archive}/content/indices/ind_close_all_{DDMMYYYY}.csv",
          lambda: nse.index_snapshots(on))
    probe("index_constituents", "{archive}/content/indices/ind_{token}list.csv",
          lambda: nse.index_constituents("nifty-50", on))
    probe("corporate_actions", "{base}/api/corporates-corporateActions?index=equities",
          lambda: nse.corporate_actions(on - dt.timedelta(days=30)))

    print(json.dumps({"asked_for": on.isoformat(), "results": results}, indent=2, default=str))
    return 0 if all(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
