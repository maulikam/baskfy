"""PM1.13: the local Screener export. Grading, an operator's cross-check and lagged history; never a live source."""

from __future__ import annotations

import calendar
import datetime as dt
import json
from pathlib import Path

DIRS = {"standalone": "companies", "consolidated": "consolidated"}
FLAG_OVER = 0.02
CRORE = 1e7


def label_to_end(label: str) -> str:
    month, year = label.split()
    m = list(calendar.month_abbr).index(month)
    return dt.date(int(year), m, calendar.monthrange(int(year), m)[1]).isoformat()


def quarters(doc: dict) -> dict[str, dict[str, float]]:
    """period_end → figures in crore, EPS in rupees. Banks and NBFCs carry `Revenue` where others carry `Sales`."""
    table = doc.get("quarters", {}).get("data") or {}
    lines = {"revenue": table.get("Sales") or table.get("Revenue") or {}, "net_profit": table.get("Net Profit", {}), "eps": table.get("EPS in Rs", {}),
             "other_income": table.get("Other Income", {}), "interest": table.get("Interest", {}), "depreciation": table.get("Depreciation", {}), "pbt": table.get("Profit before tax", {})}
    out = {}
    for label in doc.get("quarters", {}).get("headers") or []:
        out[label_to_end(label)] = {m: float(s[label]) for m, s in lines.items() if s.get(label) is not None}
    return out


def read_quarters(root: Path, basis: str, symbol: str) -> dict[str, dict[str, float]]:
    path = root / DIRS[basis] / f"{symbol}.json"
    return quarters(json.loads(path.read_text(encoding="utf8"))) if path.exists() else {}


def ttm(figures: dict[str, dict[str, float]], period_ends: list[str], metric: str) -> float | None:
    values = [figures.get(p, {}).get(metric) for p in period_ends]
    return None if any(v is None for v in values) else sum(values)


def differs(ours: float | None, theirs: float | None) -> bool:
    return ours is not None and theirs is not None and abs(theirs - ours) > FLAG_OVER * abs(ours)
