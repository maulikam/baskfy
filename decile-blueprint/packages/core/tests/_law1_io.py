"""I/O helpers for core tests — Law 1 keeps these out of ``baskfy_core`` itself (AF 3.10)."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from baskfy_core.trading_calendar import HOLIDAY_FILE, parse_seed_holidays

_REPO = Path(__file__).resolve().parents[3]  # decile-blueprint/
REFERENCE_EXPORT = _REPO / "tests" / "fixtures" / "reference-screen-export-2026-08-18.csv"


def seed_holidays() -> dict:
    text = resources.files("baskfy_core.data").joinpath(HOLIDAY_FILE).read_text(encoding="utf-8")
    return parse_seed_holidays(text)


def reference_export_path() -> Path:
    if not REFERENCE_EXPORT.is_file():
        raise FileNotFoundError(REFERENCE_EXPORT)
    return REFERENCE_EXPORT
