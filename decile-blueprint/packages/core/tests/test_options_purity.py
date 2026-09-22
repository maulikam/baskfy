"""Law 1 for ``baskfy_core.options``: no database, no network, no disk, no clock.

Asserted over the source, as ``test_vbt_purity.py`` and ``test_twt_purity.py`` do for their
packages: this code is the input to a plan the desk will confirm, and an import of
``datetime.now`` or ``sqlalchemy`` creeping in is exactly what a review misses. Also: nothing here
imports ``frozen/`` (PACK.2 — port by re-implementation), the execution package (law 2), another
book's package (Track C §8), or names an auto-execute setting (PACK.3). ``docs/options/06`` OP1.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

OPTIONS = Path(__file__).resolve().parents[1] / "src" / "baskfy_core" / "options"

FORBIDDEN = (
    re.compile(r"^\s*(from|import)\s+(sqlalchemy|httpx|requests|asyncpg|redis|celery|kiteconnect)"),
    re.compile(r"^\s*(from|import)\s+(os|pathlib|socket|subprocess|asyncio|time|random)\b"),
    re.compile(r"\.(now|today|utcnow)\("),
    re.compile(r"\bopen\("),
)

_OTHER_BOOKS = re.compile(r"^\s*(from|import)\s+baskfy_core\.(swing|vbt|twt|exposure)\b")
_FROZEN = re.compile(r"^\s*(from|import)\s+(frozen|app\.|strategies|kite_client)")


def modules() -> list[Path]:
    return sorted(OPTIONS.glob("*.py"))


def _code_lines(path: Path) -> list[tuple[int, str]]:
    return list(enumerate(path.read_text(encoding="utf-8").splitlines(), start=1))


def test_the_package_exists_and_has_its_modules() -> None:
    names = {p.stem for p in modules()}
    assert {
        "config", "calendar", "greeks", "chain", "costs", "sizing",
        "session", "risk", "journal", "execution", "backtest",
    } <= names  # fmt: skip


@pytest.mark.parametrize("path", modules(), ids=lambda p: p.name)
def test_options_module_touches_nothing(path: Path) -> None:
    offenders = [
        f"{path.name}:{n}: {line.strip()}"
        for n, line in _code_lines(path)
        for pattern in FORBIDDEN
        if pattern.search(line)
    ]
    assert offenders == []


@pytest.mark.parametrize("path", modules(), ids=lambda p: p.name)
def test_no_frozen_lab_desk_or_other_book_import(path: Path) -> None:
    offenders = [
        line for _, line in _code_lines(path) if _OTHER_BOOKS.search(line) or _FROZEN.search(line)
    ]
    assert offenders == []


def test_nothing_imports_the_execution_package() -> None:
    """The core describes orders; only ``packages/execution`` may place one (law 2)."""
    for path in modules():
        text = path.read_text(encoding="utf-8")
        assert "baskfy_execution" not in text, path.name
        assert "place_order" not in text, path.name
        assert "place_gtt" not in text, path.name


def test_no_auto_execute_setting_exists() -> None:
    """PACK.3: no auto-execute for any options sleeve, and no flag for one."""
    for path in modules():
        text = path.read_text(encoding="utf-8").upper()
        assert "AUTO_EXECUTE" not in text, path.name
        assert "AUTOEXECUTE" not in text, path.name


def test_no_literal_lot_size_in_the_package() -> None:
    """``04`` §1.4: literal lot sizes appear only in test fixtures."""
    pattern = re.compile(r"lot_size\s*[:=]\s*\d|\bLOT(_SIZE)?\s*=\s*\d")
    for path in modules():
        assert not pattern.search(path.read_text(encoding="utf-8")), path.name
