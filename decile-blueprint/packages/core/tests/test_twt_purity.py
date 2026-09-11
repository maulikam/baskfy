"""Law 1 for ``baskfy_core.twt``: no database, no network, no disk, no clock.

Asserted over the source rather than trusted, for the same reason ``test_swing_purity.py`` and
``test_vbt_purity.py`` assert it over their packages: this code is the input to a plan the desk
executes, and an import of ``datetime.now`` or ``sqlalchemy`` creeping in is exactly the kind of
thing a review misses. ``docs/twt/06`` TW1 AC 1.

The one import this package *does* make across a sleeve boundary is deliberate and is asserted
positively below: ``04`` §4.4 requires :mod:`baskfy_core.twt.breadth` to **call**
:mod:`baskfy_core.vbt.breadth` rather than restate it, and ``06`` TW1 asks the same of the
thin-session rule. Two implementations of one measurement is the single thing that can make two
pages disagree about the same day.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

TWT = Path(__file__).resolve().parents[1] / "src" / "baskfy_core" / "twt"

FORBIDDEN = (
    re.compile(r"^\s*(from|import)\s+(sqlalchemy|httpx|requests|asyncpg|redis|celery|kiteconnect)"),
    re.compile(r"^\s*(from|import)\s+(os|pathlib|socket|subprocess|asyncio)\b"),
    re.compile(r"\.(now|today|utcnow)\(\)"),
    re.compile(r"\bopen\(\s*['\"]"),
)

#: The ten modules ``docs/twt/06`` TW1 names, plus TW2's ``backtest.py`` — the sequencing of
#: ``04`` §11, which ``06`` § TW2 ships and ``06`` § TW9 re-runs over the plant's bars — plus
#: TW9's ``published.py`` and ``drift.py``: ``01`` §6's measurements as a record, and the
#: comparison ``03`` §9 stores against them (DECISIONS-TW **TW9.1**; the same pair
#: ``baskfy_core.vbt`` carries, for the same reason). The set is exact on purpose: a module that
#: nobody decided on fails here.
EXPECTED_MODULES = frozenset(
    {
        "__init__.py",
        "backtest.py",
        "breadth.py",
        "calendar.py",
        "config.py",
        "drift.py",
        "exits.py",
        "indicators.py",
        "plan.py",
        "published.py",
        "signals.py",
        "sizing.py",
        "sleeve.py",
    }
)


def modules() -> list[Path]:
    return sorted(TWT.glob("*.py"))


def _imports(path: Path) -> set[str]:
    """Every module this file actually imports, over the syntax tree rather than the text.

    Both packages cross-reference each other in **prose** — ``twt/sizing.py`` explains how its cap
    order differs from VBT-1's, which is exactly the kind of comment that should be encouraged. A
    cross-reference is not a dependency; an import is.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_the_package_has_every_module_the_plan_names() -> None:
    assert {path.name for path in modules()} == EXPECTED_MODULES


@pytest.mark.parametrize("path", modules(), ids=lambda p: p.name)
def test_twt_module_touches_nothing(path: Path) -> None:
    offenders = [
        f"{path.name}:{number}: {line.strip()}"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        for pattern in FORBIDDEN
        if pattern.search(line)
    ]
    assert offenders == []


def test_twt_does_not_import_the_desk_or_the_execution_package() -> None:
    """The plan describes orders; only ``packages/execution`` may place one (law 2)."""
    for path in modules():
        text = path.read_text(encoding="utf-8")
        assert "baskfy_execution" not in text, path.name
        assert "kite_client" not in text, path.name


def test_twt_does_not_import_the_swing_package() -> None:
    """Track C §5: this sleeve shares no rule with the swing book and runs none of its code."""
    for path in modules():
        offending = {name for name in _imports(path) if name.startswith("baskfy_core.swing")}
        assert offending == set(), f"{path.name} imports {offending}"


def test_the_breadth_module_calls_vbt_rather_than_copying_it() -> None:
    """``04`` §4.4 and DECISIONS-TW TW0.4: one arithmetic, two rows."""
    called = _imports(TWT / "breadth.py")
    assert "baskfy_core.vbt.breadth" in called
    assert "baskfy_core.vbt.config" in called


def test_the_calendar_module_calls_vbt_rather_than_copying_it() -> None:
    """``06`` TW1: the thin-session rule is shared with VBT-1's implementation."""
    assert "baskfy_core.vbt.calendar" in _imports(TWT / "calendar.py")


def test_no_auto_execute_setting_exists() -> None:
    """``02`` Track C §3 — non-negotiable 1's named exception is the *swing* sleeve's, by Maulik's
    own hand. An agent may not widen it, add a second one, or default a flag to true."""
    for path in modules():
        text = path.read_text(encoding="utf-8").upper()
        assert "AUTO_EXECUTE" not in text, path.name
        assert "AUTOEXECUTE" not in text, path.name


def test_the_look_ahead_reading_is_not_reachable_from_the_package() -> None:
    """DECISIONS-TW **TW0.1**. ``tight_state`` takes no ``include_current_week`` switch, no
    ``lookahead=`` keyword and there is no ``TightReading`` class: the look-ahead reading is a
    different function, and it lives in TW2's test module because the sleeve must never be able to
    run by accident a signal the market never offered.

    Over the syntax tree, never the text. The package's *prose* names the switch it does not have —
    saying why a thing is absent is how the next session finds the decision instead of adding it
    back — and a scan wide enough to catch that paragraph is a scan that discourages the paragraph.
    """
    forbidden = ("include_current_week", "lookahead", "look_ahead", "tightreading")
    for path in modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        named: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                named.add(node.name)
                named.update(arg.arg for arg in (*node.args.args, *node.args.kwonlyargs))
            elif isinstance(node, ast.ClassDef):
                named.add(node.name)
            elif isinstance(node, ast.Name):
                named.add(node.id)
            elif isinstance(node, ast.Attribute):
                named.add(node.attr)
        offending = {name for name in named if any(bad in name.lower() for bad in forbidden)}
        assert offending == set(), f"{path.name} names {offending}"
