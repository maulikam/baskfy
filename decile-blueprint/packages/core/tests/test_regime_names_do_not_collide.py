"""Two different things were both called "regime", and this stops them merging back (docs/03 §3c).

`baskfy_core.instrument_regime` labels ONE INSTRUMENT bull / bear / neutral from the Wasserstein
distance between its recent returns and its own best and worst periods. It is a display factor on
the factsheet, and it decides nothing.

The desk's exposure overlay — `app/core/regime.py`, bound for `baskfy_core.exposure` — is a
PORTFOLIO-level R1-R4 tier that decides **how much money is deployed**. Confusing the two is not a
naming quibble: one is a label next to a stock, the other sizes the book.

docs/03 §3c: "Keep both. Rename at merge time. Never let one import the other's name."
"""

from __future__ import annotations

import ast
import pathlib

import pytest

CORE_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "baskfy_core"

#: The name that must never come back. `regime` alone is ambiguous by construction.
BANNED_MODULE = "baskfy_core.regime"


def _python_files() -> list[pathlib.Path]:
    return sorted(p for p in CORE_SRC.rglob("*.py") if "__pycache__" not in p.parts)


def test_there_are_files_to_scan() -> None:
    assert _python_files(), "the scan found nothing, so it proves nothing"


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_nothing_imports_the_ambiguous_name(path: pathlib.Path) -> None:
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == BANNED_MODULE:
            raise AssertionError(
                f"{path.name} imports {BANNED_MODULE}. That module was renamed to "
                f"baskfy_core.instrument_regime at M15 because the desk's exposure overlay is "
                f"also called 'regime' and means something else entirely."
            )
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != BANNED_MODULE, f"{path.name} imports {BANNED_MODULE}"


def test_no_module_holds_both_concepts_at_once() -> None:
    """The failure mode docs/03 §3c actually warns about: one file reasoning about both.

    Checked on IMPORTS, not on the source text. Several modules name both concepts in prose --
    that is how a reader learns they are different -- and a text scan would flag the very
    documentation that prevents the confusion.
    """
    offenders = []
    for path in _python_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
            elif isinstance(node, ast.Import):
                modules.update(a.name for a in node.names)
        instrument = any("instrument_regime" in m for m in modules)
        exposure = any(m.startswith("baskfy_core.exposure") for m in modules)
        if instrument and exposure:
            offenders.append(path.name)
    assert offenders == [], (
        f"{offenders} import both regime concepts. If that is genuinely needed, alias them at the "
        f"import so the reader can tell which is which."
    )


def test_the_renamed_module_is_the_one_that_exists() -> None:
    assert (CORE_SRC / "instrument_regime.py").is_file()
    assert not (CORE_SRC / "regime.py").exists()
