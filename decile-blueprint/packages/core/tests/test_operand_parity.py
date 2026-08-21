"""The web app's custom-filter operand list matches the registry's (Prompt 9 deliverable 8).

docs/01 §2.14 gives the screener three custom-filter slots whose operands "are drawn from the same
list", and docs/06 §"The factor registry" makes the registry "the single source of truth for: ...
the custom-filter operand list". `baskfy_core.factor_registry.CUSTOM_FILTER_OPERANDS` is that
list, and `baskfy_core.screener` refuses anything outside it before a statement is built.

docs/07 §Metadata enumerates five `/meta/*` endpoints and none of them publishes this list, so the
browser cannot fetch it — `apps/web/src/lib/screens/operands.ts` carries its own copy. This test is
the link between the two. Without it, the UI could offer an operand the API answers 400 for, or
quietly omit one it accepts, and nothing would notice until a user tried it.

Same arrangement as `test_screen_definition_parity.py`, which keeps the Zod mirror honest.
"""

from __future__ import annotations

import re
from pathlib import Path

from baskfy_core.factor_registry import CUSTOM_FILTER_OPERANDS
from baskfy_core.models import FactorDaily

REPO_ROOT = Path(__file__).resolve().parents[3]
OPERANDS_TS = REPO_ROOT / "apps" / "web" / "src" / "lib" / "screens" / "operands.ts"

_ARRAY = re.compile(r"CUSTOM_FILTER_OPERAND_KEYS\s*=\s*\[(.*?)\]\s*as const", re.DOTALL)
_ENTRY = re.compile(r'"([a-z0-9_]+)"')


def typescript_operands() -> tuple[str, ...]:
    source = OPERANDS_TS.read_text(encoding="utf-8")
    match = _ARRAY.search(source)
    assert match is not None, f"CUSTOM_FILTER_OPERAND_KEYS not found in {OPERANDS_TS}"
    return tuple(_ENTRY.findall(match.group(1)))


def test_the_typescript_file_exists() -> None:
    assert OPERANDS_TS.is_file(), f"{OPERANDS_TS} is missing"


def test_the_lists_are_identical() -> None:
    """Same keys, same order — the order is the order docs/01 §2.14 prints them in."""
    assert typescript_operands() == CUSTOM_FILTER_OPERANDS


def test_every_operand_is_a_real_factor_daily_column() -> None:
    """A key that is not a column would build SQL that does not compile."""
    columns = {column.name for column in FactorDaily.__table__.c}
    unknown = [key for key in CUSTOM_FILTER_OPERANDS if key not in columns]
    assert unknown == []


def test_docs_01_names_twenty_two_operands() -> None:
    """1 return + 5 volatilities + beta + 2 closes + 2 away-from-highs + 4 MAs + 7 volumes."""
    assert len(CUSTOM_FILTER_OPERANDS) == 22
