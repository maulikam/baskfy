"""`docs/swing/04-business-rules.md` is the contract; this asserts the code still matches it.

    "Every number here is a field of `baskfy_core.swing.config` ... The tests in
     `packages/core/tests/test_swing_*.py` assert **this document**; when a module finds the
     document and the code disagreeing, the document wins and the code is fixed."

The other swing test modules assert the *values*. This one asserts the *vocabulary*: every
threshold the engine reads is named in the document, and every setup, status, skip reason and
action the engine can emit is named there too. A field renamed in a refactor without the
document being edited leaves the document describing a system that no longer exists — which is
the failure mode a spec-first repository is most exposed to, because nothing else breaks.

Direction matters. This checks code → document (a new or renamed knob must be written down),
not document → code: `04` also names quantities that are columns rather than config fields
(`range_pct`, `close_position`), and prose is allowed to be richer than the dataclasses.

SW1, `docs/swing/06-module-plan.md`.
"""

from __future__ import annotations

import ast
import dataclasses
import re
from pathlib import Path
from typing import Final

import pytest

from baskfy_core.swing import config as swing_config
from baskfy_core.swing.config import DEFAULT_SWING_CONFIG, TRADEABLE_SETUPS, Setup
from baskfy_core.swing.market import MarketGate
from baskfy_core.swing.plan import LineKind, SkipReason
from baskfy_core.swing.setups import CandidateStatus
from baskfy_core.swing.stops import ActionKind, ActionReason, StopMode, TrailMa


#: The document this module is the parity check for. Found by walking up to the repository
#: root rather than by a fixed number of `..` hops, so moving the package does not silently
#: turn this test into a skip.
def _docs_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "docs" / "swing" / "04-business-rules.md"
        if candidate.is_file():
            return candidate
    raise AssertionError(
        "docs/swing/04-business-rules.md was not found above this file; the numerical "
        "contract is missing, which is a worse problem than a failing assertion."
    )


RULES: Final[str] = _docs_root().read_text(encoding="utf-8")


def _config_fields() -> list[tuple[str, str]]:
    """Every ``(dataclass, field)`` pair declared in ``baskfy_core.swing.config``.

    Read from the source with ``ast`` rather than from ``dataclasses.fields`` so that a field
    which exists only as an annotation — and a class that is not itself reachable from
    :data:`DEFAULT_SWING_CONFIG` — is still covered.
    """
    source = Path(swing_config.__file__).read_text(encoding="utf-8")
    pairs: list[tuple[str, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ClassDef):
            continue
        for statement in node.body:
            if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                pairs.append((node.name, statement.target.id))
    return pairs


CONFIG_FIELDS: Final[list[tuple[str, str]]] = _config_fields()


def _mentions(name: str) -> bool:
    """Is ``name`` written in the document as a word, not as a fragment of a longer one?

    ``\\b`` alone is not enough: ``min_rvol`` is a substring of nothing here, but
    ``ma_fast`` is a substring of ``index_ma_fast`` and a plain ``in`` test would let a
    renamed ``ma_fast`` pass because the index knob happens to be documented.
    """
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", RULES) is not None


class TestEveryThresholdIsWrittenDown:
    def test_the_config_module_declares_fields_at_all(self) -> None:
        """A guard on the guard: an ast walk that finds nothing would pass every test below."""
        assert len(CONFIG_FIELDS) > 50, (
            f"only {len(CONFIG_FIELDS)} config fields were parsed out of "
            f"{swing_config.__file__}; the parity check is not reading the module it thinks"
        )

    @pytest.mark.parametrize(("owner", "field"), CONFIG_FIELDS, ids=str)
    def test_field_is_named_in_the_business_rules(self, owner: str, field: str) -> None:
        assert _mentions(field), (
            f"{owner}.{field} is a threshold the engine reads and "
            f"docs/swing/04-business-rules.md does not name it. The document is the contract: "
            f"add the field to §1-§11 (with its default) in the same change that adds the field."
        )

    def test_the_top_level_config_groups_are_named(self) -> None:
        """`04`'s preamble lists the groups by name; a renamed group must be renamed there."""
        for group in (f.name for f in dataclasses.fields(DEFAULT_SWING_CONFIG)):
            assert _mentions(group), f"SwingConfig.{group} is not named in the business rules"


class TestEveryVocabularyValueIsWrittenDown:
    """The strings that leave the engine — they end up in a database column and on a page."""

    @pytest.mark.parametrize(
        "member",
        [
            *Setup,
            *CandidateStatus,
            *MarketGate,
            *LineKind,
            *SkipReason,
            *ActionKind,
            *ActionReason,
            *TrailMa,
            *StopMode,
        ],
        ids=lambda m: f"{type(m).__name__}.{m.name}",
    )
    def test_enum_value_is_named_in_the_business_rules(self, member: object) -> None:
        value = str(member)
        assert _mentions(value), (
            f"{type(member).__name__}.{value} can be written to a `sw_` row or shown on a "
            f"page, and docs/swing/04-business-rules.md never mentions it."
        )

    def test_the_untradeable_setup_is_documented_as_untradeable(self) -> None:
        """PACK.1 and Track C: the document must still say the short is detect-only."""
        excluded = sorted(set(Setup) - set(TRADEABLE_SETUPS))
        assert [s.value for s in excluded] == ["PARABOLIC_SHORT"]
        assert "TRADEABLE_SETUPS" in RULES
        assert "detect only" in RULES or "detect-only" in RULES
