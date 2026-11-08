"""Load the committed reference CSV — Law 1 keeps this out of baskfy_core (AF 3.10)."""

from __future__ import annotations

from pathlib import Path

from baskfy_core.reference_export import ReferenceRows, read_export, to_rows

# packages/providers/src/baskfy_providers/ → parents[4] = decile-blueprint
_REPO = Path(__file__).resolve().parents[4]
REFERENCE_EXPORT = _REPO / "tests" / "fixtures" / "reference-screen-export-2026-08-18.csv"


def reference_export_path() -> Path:
    if not REFERENCE_EXPORT.is_file():
        raise FileNotFoundError(REFERENCE_EXPORT)
    return REFERENCE_EXPORT


def reference_rows() -> ReferenceRows:
    return to_rows(read_export(reference_export_path()))
