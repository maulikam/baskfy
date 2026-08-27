"""The landing page's factor-family table matches the registry (Prompt 18 deliverable 1).

PROMPTS.md Prompt 18 asks the marketing page to show "the factor families". The registry is the
single source of truth for what those are (docs/06 §"The factor registry"), and no ``/meta/*``
endpoint publishes a per-family *count* — ``GET /meta/factors`` returns every factor with its
family, which the landing page deliberately does not fetch: a marketing page that cannot render
without the API is a marketing page that is blank exactly when someone is deciding whether to
trust the service.

So ``apps/web/src/lib/marketing/factor-families.ts`` carries its own copy, and this test is the
link — the same arrangement as :mod:`test_operand_parity`, which keeps the custom-filter operand
list honest. A family renamed, added, or grown by a factor and not reflected on the landing page
fails here rather than quietly advertising a number that stopped being true.
"""

from __future__ import annotations

import re
from pathlib import Path

from baskfy_core.factor_registry import FactorFamily, by_family

REPO_ROOT = Path(__file__).resolve().parents[3]
FAMILIES_TS = REPO_ROOT / "apps" / "web" / "src" / "lib" / "marketing" / "factor-families.ts"

_ENTRY = re.compile(
    r'key:\s*"(?P<key>[a-z_]+)".*?count:\s*(?P<count>\d+)',
    re.DOTALL,
)


def typescript_families() -> tuple[tuple[str, int], ...]:
    source = FAMILIES_TS.read_text(encoding="utf-8")
    return tuple(
        (match.group("key"), int(match.group("count"))) for match in _ENTRY.finditer(source)
    )


def test_the_typescript_file_exists() -> None:
    assert FAMILIES_TS.is_file(), f"{FAMILIES_TS} is missing"


def test_every_family_appears_exactly_once_in_registry_order() -> None:
    assert [key for key, _ in typescript_families()] == [family.value for family in FactorFamily]


def test_each_advertised_count_is_the_registry_count() -> None:
    registry = {family.value: len(factors) for family, factors in by_family().items()}
    assert dict(typescript_families()) == registry


def test_the_advertised_total_is_the_registry_total() -> None:
    """docs/01 §3 is headed "62 ranking factors" and enumerates 64. We implement all of them.

    Was a literal 64. Stated against the registry now, because the marketing page and the registry
    disagreeing is the *only* failure this can usefully catch — and a hard-coded total has to be
    re-pinned by hand on every deliberate addition, which is how a guard becomes a chore and then
    a rubber stamp. M56 added two (`avg_sharpe_3_1`, `avg_sharpe_6_1`); the count the page shows
    a visitor must move with them.
    """
    registry_total = sum(len(factors) for factors in by_family().values())
    assert sum(count for _, count in typescript_families()) == registry_total
