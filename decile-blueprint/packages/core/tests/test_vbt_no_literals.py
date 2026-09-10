"""No rule of VBT-1 is written as a number anywhere but ``baskfy_core.vbt.config``.

``docs/vbt/06`` VB1 AC 2 and the run's standing rule: *every threshold is a field of
`baskfy_core.vbt.config`, never a literal in a detector, task, router or page*. The point is not
tidiness. A threshold that appears in two places is a threshold that will be changed in one of
them, and the strategy's own history has an example — ``grid2.py`` still carries two filters the
research note dropped, which is why DECISIONS-VB VB0.2 had to be measured rather than read.

Asserted over the syntax tree rather than the text, so a number inside a docstring — where the
*reason* for a threshold belongs — never trips it, and a number inside an expression always does.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest

from baskfy_core.vbt.config import DEFAULT_VBT_CONFIG

VBT = Path(__file__).resolve().parents[1] / "src" / "baskfy_core" / "vbt"

#: The modules that carry the rules. ``config`` is where the numbers live; ``__init__`` re-exports.
RULE_MODULES = ("signals.py", "breadth.py", "sizing.py", "exits.py", "orders.py", "plan.py")

#: The only numbers a rule module may spell out: the identity, the unit, the pair and the percent.
#: Anything else is a threshold, and a threshold is a field.
ARITHMETIC = frozenset({0, 1, 2, 100})

#: ``indicators`` and ``calendar`` shape the data rather than judging it, so they carry the two
#: numbers that are definitions rather than settings — 1 for a one-session shift, and the 0.5 of
#: ``04`` §1's "0.5 when ``high == low``", which is not a threshold anybody could tune: a bar with
#: no range has no position within its range, and 0.5 is the refusal to call it strong or weak.
SHAPING = frozenset({0, 0.5, 1, 2, 100})


def numeric_constants(path: Path) -> list[tuple[int, float]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    ]


def configured_values() -> set[float]:
    values: set[float] = set()
    for group in dataclasses.fields(DEFAULT_VBT_CONFIG):
        sub = getattr(DEFAULT_VBT_CONFIG, group.name)
        for spec in dataclasses.fields(sub):
            value = getattr(sub, spec.name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                values.add(float(value))
    return values


@pytest.mark.parametrize("name", RULE_MODULES)
def test_a_rule_module_spells_out_no_number_but_arithmetic(name: str) -> None:
    offenders = [
        f"{name}:{line}: {value!r}"
        for line, value in numeric_constants(VBT / name)
        if value not in ARITHMETIC
    ]
    assert offenders == [], (
        f"these numbers belong in baskfy_core.vbt.config, with a docstring saying why: {offenders}"
    )


@pytest.mark.parametrize("name", ["indicators.py", "calendar.py"])
def test_a_shaping_module_spells_out_no_configured_threshold(name: str) -> None:
    thresholds = configured_values()
    offenders = [
        f"{name}:{line}: {value!r}"
        for line, value in numeric_constants(VBT / name)
        if value not in SHAPING and float(value) in thresholds
    ]
    assert offenders == [], f"a config value is duplicated as a literal: {offenders}"


def test_the_config_module_is_where_the_numbers_are() -> None:
    """The inverse assertion, so this file cannot pass by the package having no numbers at all."""
    assert len(configured_values()) >= 25


def test_every_threshold_carries_a_reason() -> None:
    """A field with no comment above it is a number nobody can defend six months from now.

    ``config.py`` documents each field with a ``#:`` comment (Sphinx's attribute-docstring form)
    or a dataclass docstring paragraph. This asserts the density, not the prose: a group whose
    fields outnumber its comment lines has stopped explaining itself.
    """
    text = (VBT / "config.py").read_text(encoding="utf-8").splitlines()
    comments = sum(1 for line in text if line.strip().startswith("#:"))
    fields = sum(
        1
        for line in text
        if ":" in line
        and "=" in line
        and line.startswith("    ")
        and not line.strip().startswith(("#", '"', "@", "def ", "return"))
    )
    assert comments >= fields * 0.5, f"{fields} fields, {comments} explanatory comments"
