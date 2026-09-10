"""Law 1 for ``baskfy_core.vbt``: no database, no network, no disk, no clock.

Asserted over the source rather than trusted, for the same reason ``test_swing_purity.py``
asserts it over the swing package: this code is the input to a plan the desk executes, and an
import of ``datetime.now`` or ``sqlalchemy`` creeping in is exactly the kind of thing a review
misses. ``docs/vbt/06`` VB1 AC 1.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

VBT = Path(__file__).resolve().parents[1] / "src" / "baskfy_core" / "vbt"

FORBIDDEN = (
    re.compile(r"^\s*(from|import)\s+(sqlalchemy|httpx|requests|asyncpg|redis|celery|kiteconnect)"),
    re.compile(r"^\s*(from|import)\s+(os|pathlib|socket|subprocess|asyncio)\b"),
    re.compile(r"\.(now|today|utcnow)\(\)"),
    re.compile(r"\bopen\(\s*['\"]"),
)


def modules() -> list[Path]:
    return sorted(VBT.glob("*.py"))


def test_the_package_exists_and_has_modules() -> None:
    assert len(modules()) >= 9


@pytest.mark.parametrize("path", modules(), ids=lambda p: p.name)
def test_vbt_module_touches_nothing(path: Path) -> None:
    offenders = [
        f"{path.name}:{number}: {line.strip()}"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        for pattern in FORBIDDEN
        if pattern.search(line)
    ]
    assert offenders == []


def test_vbt_does_not_import_the_desk_or_the_execution_package() -> None:
    """The plan describes orders; only ``packages/execution`` may place one (law 2)."""
    for path in modules():
        text = path.read_text(encoding="utf-8")
        assert "baskfy_execution" not in text, path.name
        assert "kite_client" not in text, path.name


_SWING_IMPORT = re.compile(r"^\s*(from|import)\s+baskfy_core\.swing")


def test_vbt_does_not_import_the_swing_package() -> None:
    """Track C §5: this sleeve shares no rule with the swing book and runs none of its code.

    Prose may *mention* it — the two packages are deliberately the same shape, and saying so is
    how a reader finds the precedent. What may not happen is an import.
    """
    for path in modules():
        offenders = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if _SWING_IMPORT.search(line)
        ]
        assert offenders == [], path.name


def test_no_auto_execute_setting_exists() -> None:
    """``02`` Track C §3 and DECISIONS-VB PACK.2 — non-negotiable 1's exception is the swing
    sleeve's, and this run neither widens it nor adds a second one."""
    for path in modules():
        text = path.read_text(encoding="utf-8").upper()
        assert "AUTO_EXECUTE" not in text, path.name
        assert "AUTOEXECUTE" not in text, path.name
