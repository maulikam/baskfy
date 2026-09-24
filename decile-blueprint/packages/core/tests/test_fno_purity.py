"""Law 1 for ``baskfy_core.fno``: no database, no network, no disk, no clock (``docs/fno/06`` FO1).

Mirrors ``test_options_purity.py``. The FO core is the input to a plan that carries a derivative
across the close, so an import of ``datetime.now`` or ``sqlalchemy`` creeping in is exactly what a
review misses. Also: nothing here imports ``frozen/``, the desk, the execution package (law 2),
another book's package (Track C §7), or reads env — the ceilings are handed in — and no
auto-execute setting is named (``02`` Track B).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

FNO = Path(__file__).resolve().parents[1] / "src" / "baskfy_core" / "fno"

FORBIDDEN = (
    re.compile(r"^\s*(from|import)\s+(sqlalchemy|httpx|requests|asyncpg|redis|celery|kiteconnect)"),
    re.compile(r"^\s*(from|import)\s+(os|pathlib|socket|subprocess|asyncio|time|random)\b"),
    re.compile(r"\.(now|today|utcnow)\("),
    re.compile(r"\bopen\("),
    re.compile(r"\b(read_parquet|scan_parquet|read_csv|write_parquet|write_csv)\b"),
    re.compile(r"\b(getenv|environ)\b"),
)

_OTHER_BOOKS = re.compile(r"^\s*(from|import)\s+baskfy_core\.(swing|vbt|twt|exposure|score)\b")
_FROZEN = re.compile(r"^\s*(from|import)\s+(frozen|app\.|app\b|strategies|kite_client)")


def modules() -> list[Path]:
    return sorted(FNO.glob("*.py"))


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def test_the_package_exists_and_has_its_modules() -> None:
    names = {p.stem for p in modules()}
    assert {
        "config", "series", "vol", "calendar", "condor", "covered",
        "sizing", "costs", "exits", "research",
    } <= names  # fmt: skip


@pytest.mark.parametrize("path", modules(), ids=lambda p: p.name)
def test_fno_module_touches_nothing(path: Path) -> None:
    offenders = [
        f"{path.name}:{n}: {line.strip()}"
        for n, line in enumerate(_lines(path), start=1)
        for pattern in FORBIDDEN
        if pattern.search(line)
    ]
    assert offenders == []


@pytest.mark.parametrize("path", modules(), ids=lambda p: p.name)
def test_no_frozen_lab_desk_or_other_book_import(path: Path) -> None:
    offenders = [line for line in _lines(path) if _OTHER_BOOKS.search(line) or _FROZEN.search(line)]
    assert offenders == []


def test_nothing_imports_the_execution_package() -> None:
    for path in modules():
        text = path.read_text(encoding="utf-8")
        assert "baskfy_execution" not in text, path.name
        assert "place_order" not in text, path.name
        assert "place_gtt" not in text, path.name


def test_no_auto_execute_setting_exists() -> None:
    """``02`` Track B: no ``BASKFY_FNO_*AUTO*`` name, and no field for one."""
    for path in modules():
        text = path.read_text(encoding="utf-8").upper()
        assert "AUTO_EXECUTE" not in text, path.name
        assert "AUTOEXECUTE" not in text, path.name
        assert not re.search(r"BASKFY_FNO_\w*AUTO", text), path.name
