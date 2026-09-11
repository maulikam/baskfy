"""No rule of TWT-1 is written as a number anywhere but ``baskfy_core.twt.config``.

``docs/twt/04``'s opening sentence and ``docs/twt/06`` TW1's G7: *every threshold is a field of
`baskfy_core.twt.config` and nothing downstream compares to a literal*. **This is the gate that
keeps ``04`` the spec rather than a description of the code.** A threshold that appears in two
places is a threshold that will be changed in one of them, and this repository already has the
example: ``grid2.py`` still carries two filters the VBT research note dropped, which is why
DECISIONS-VB VB0.2 had to be measured rather than read.

Asserted over the syntax tree rather than the text, so a number inside a docstring — where the
*reason* for a threshold belongs, and where ``04``'s sensitivity figures are quoted on purpose —
never trips it, and a number inside an expression always does.
"""

from __future__ import annotations

import ast
import dataclasses
from decimal import Decimal
from pathlib import Path

import pytest

from baskfy_core.twt.config import DEFAULT_TWT_CONFIG

TWT = Path(__file__).resolve().parents[1] / "src" / "baskfy_core" / "twt"
REPO = Path(__file__).resolve().parents[4]

#: The modules that carry the rules. ``config`` is where the numbers live; ``__init__`` re-exports.
RULE_MODULES = ("signals.py", "breadth.py", "sizing.py", "exits.py", "plan.py", "sleeve.py")

#: The only numbers a rule module may spell out: the identity, the unit, the pair and the percent.
#: Anything else is a threshold, and a threshold is a field.
ARITHMETIC = frozenset({0, 1, 2, 100})

#: ``indicators`` and ``calendar`` shape the data rather than judging it, so they may carry the
#: numbers that are **definitions**: the 12 months of a year and the 100 that scales an ISO year
#: into a week key. Neither is a threshold anybody could tune.
SHAPING = frozenset({0, 1, 2, 12, 100})


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
    """Every threshold the config holds, as a float, so a literal can be recognised as one."""
    values: set[float] = set()
    for group in dataclasses.fields(DEFAULT_TWT_CONFIG):
        sub = getattr(DEFAULT_TWT_CONFIG, group.name)
        for spec in dataclasses.fields(sub):
            value = getattr(sub, spec.name)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float, Decimal)):
                values.add(float(value))
    return values


@pytest.mark.parametrize("name", RULE_MODULES)
def test_a_rule_module_spells_out_no_number_but_arithmetic(name: str) -> None:
    offenders = [
        f"{name}:{line}: {value!r}"
        for line, value in numeric_constants(TWT / name)
        if value not in ARITHMETIC
    ]
    assert offenders == [], (
        f"these numbers belong in baskfy_core.twt.config, with a docstring saying why: {offenders}"
    )


@pytest.mark.parametrize("name", ["indicators.py", "calendar.py"])
def test_a_shaping_module_spells_out_no_configured_threshold(name: str) -> None:
    thresholds = configured_values()
    offenders = [
        f"{name}:{line}: {value!r}"
        for line, value in numeric_constants(TWT / name)
        if value not in SHAPING and float(value) in thresholds
    ]
    assert offenders == [], f"a config value is duplicated as a literal: {offenders}"


def test_the_config_module_is_where_the_numbers_are() -> None:
    """The inverse assertion, so this file cannot pass by the package having no numbers at all."""
    assert len(configured_values()) >= 25


def test_every_threshold_carries_a_reason() -> None:
    """A field with no comment above it is a number nobody can defend six months from now.

    ``config.py`` documents each field with a ``#:`` comment (Sphinx's attribute-docstring form) or
    a dataclass docstring paragraph. This asserts the density, not the prose: a group whose fields
    outnumber its comment lines has stopped explaining itself.
    """
    text = (TWT / "config.py").read_text(encoding="utf-8").splitlines()
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


class TestTheResearchOnlyFields:
    """``04`` §3.5 and DECISIONS-TW **TW0.3**: ``research_min_turnover_inr`` exists for exactly one
    caller, TW2's golden parameter set. It is never read by the detector, the plan or a page."""

    #: The directories a production reference would live in. TW2's goldens live under ``tools/twt``
    #: and the tests, which are the two places the research value is allowed to appear.
    PRODUCTION = (
        REPO / "packages" / "core" / "src",
        REPO / "packages" / "providers" / "src",
        REPO / "services" / "api" / "src",
        REPO / "services" / "worker" / "src",
    )

    @pytest.mark.parametrize("field", ["research_min_turnover_inr", "RESEARCH_TICK_INR"])
    def test_the_only_production_reference_is_the_config_module_itself(self, field: str) -> None:
        offenders = [
            str(path.relative_to(REPO))
            for root in self.PRODUCTION
            if root.exists()
            for path in root.rglob("*.py")
            if "__pycache__" not in path.parts
            and path != TWT / "config.py"
            and field in path.read_text(encoding="utf-8")
        ]
        assert offenders == [], (
            f"{field} is the research's number, not this sleeve's; only TW2's goldens may read it. "
            f"Found in {offenders}"
        )

    def test_the_shipped_floor_and_the_research_floor_are_different_numbers(self) -> None:
        entry = DEFAULT_TWT_CONFIG.entry
        assert entry.min_turnover_inr == Decimal("50000000")
        assert entry.research_min_turnover_inr == Decimal("20000000")
        assert entry.min_turnover_inr > entry.research_min_turnover_inr
