"""Law 1 for ``baskfy_core.swing``: no database, no network, no disk, no clock.

Asserted over the source rather than trusted: the swing package is the input to a live
monitor and a plan the desk executes, and an import of ``datetime.now`` or ``sqlalchemy``
creeping in is exactly the kind of thing a review misses.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SWING = Path(__file__).resolve().parents[1] / "src" / "baskfy_core" / "swing"

FORBIDDEN = (
    re.compile(r"^\s*(from|import)\s+(sqlalchemy|httpx|requests|asyncpg|redis|celery|kiteconnect)"),
    re.compile(r"^\s*(from|import)\s+(os|pathlib|socket|subprocess|asyncio)\b"),
    re.compile(r"\.(now|today|utcnow)\(\)"),
    re.compile(r"\bopen\(\s*['\"]"),
)


def modules() -> list[Path]:
    return sorted(SWING.glob("*.py"))


def test_the_package_exists_and_has_modules() -> None:
    assert len(modules()) >= 9


@pytest.mark.parametrize("path", modules(), ids=lambda p: p.name)
def test_swing_module_touches_nothing(path: Path) -> None:
    offenders = [
        f"{path.name}:{number}: {line.strip()}"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        for pattern in FORBIDDEN
        if pattern.search(line)
    ]
    assert offenders == []


def test_swing_does_not_import_the_desk_or_the_execution_package() -> None:
    """The plan describes orders; only ``packages/execution`` may place one (law 2)."""
    for path in modules():
        text = path.read_text(encoding="utf-8")
        assert "baskfy_execution" not in text, path.name
        assert "kite_client" not in text, path.name
