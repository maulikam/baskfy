"""One place that answers "is the options lab present?" (M6).

The strangle subsystem is frozen in `frozen/strangle/` and is outside every gate. Tests that
exercise it are not deleted and not weakened — they are **skipped, with the reason attached**,
and they run again the moment the subsystem is thawed. That is the same posture both
repositories already take toward things that cannot run: say so, do not quietly pass.

Import `options_lab` and decorate. Do not inline `importorskip` at call sites: the reason
string is the useful part, and one copy of it stays true.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest


def _present() -> bool:
    """Both halves must be there: the package the code imports and the runnable scripts."""
    if importlib.util.find_spec("app.strategies.strangle.book") is None:
        return False
    return pathlib.Path("scripts/strangle.py").exists()


PRESENT = _present()

options_lab = pytest.mark.skipif(
    not PRESENT,
    reason="the options lab is frozen (frozen/strangle/, M6) — thaw it to run this",
)
