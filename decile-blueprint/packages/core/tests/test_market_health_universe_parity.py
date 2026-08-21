"""The web app's market-health selector matches the twelve universes that carry breadth.

docs/01 §6 lists the selector's twelve slugs by name. `baskfy_core.universes` marks which of the
fourteen selectable universes those are (``Universe.market_health``), and
`baskfy_api.routers.market_data` refuses a request for one of the other two.

docs/07 §Metadata publishes `/meta/universes`, but it publishes all fourteen and says nothing
about which have a breadth series — so the browser cannot derive the selector from it, and
`apps/web/src/lib/market/universes.ts` carries its own copy. This test is the link. Without it the
selector could offer `etf`, which the API answers 400 for, and nothing would notice until someone
picked it.

Same arrangement as `test_operand_parity.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

from baskfy_core.universes import MARKET_HEALTH_SLUGS, UNIVERSE_BY_SLUG

REPO_ROOT = Path(__file__).resolve().parents[3]
UNIVERSES_TS = REPO_ROOT / "apps" / "web" / "src" / "lib" / "market" / "universes.ts"

_ARRAY = re.compile(r"HEALTH_UNIVERSES:.*?=\s*\[(.*?)\]\s*as const", re.DOTALL)
_ENTRY = re.compile(r'\{\s*slug:\s*"([a-z0-9-]+)",\s*name:\s*"([^"]+)"\s*\}')

#: docs/01 §6, verbatim and in its own order.
DOCUMENTED_SLUGS: tuple[str, ...] = (
    "nifty-allcap",
    "nifty-50",
    "nifty-next-50",
    "nifty-100",
    "nifty-200",
    "nifty-500",
    "nifty-total-market",
    "nifty-large-mid-250",
    "nifty-midcap-150",
    "nifty-smallcap-250",
    "nifty-microcap-250",
    "nifty-mid-small-400",
)


def typescript_options() -> tuple[tuple[str, str], ...]:
    source = UNIVERSES_TS.read_text(encoding="utf-8")
    match = _ARRAY.search(source)
    assert match is not None, f"HEALTH_UNIVERSES not found in {UNIVERSES_TS}"
    return tuple(_ENTRY.findall(match.group(1)))


def test_the_typescript_file_exists() -> None:
    assert UNIVERSES_TS.is_file(), f"{UNIVERSES_TS} is missing"


def test_docs_01_lists_exactly_the_universes_marked_for_breadth() -> None:
    """The document and the dataclass table agree on which twelve they are."""
    assert set(DOCUMENTED_SLUGS) == set(MARKET_HEALTH_SLUGS)
    assert len(DOCUMENTED_SLUGS) == len(MARKET_HEALTH_SLUGS) == 12


def test_the_selector_offers_exactly_those_twelve_in_the_documented_order() -> None:
    assert tuple(slug for slug, _ in typescript_options()) == DOCUMENTED_SLUGS


def test_every_option_carries_the_universes_own_name() -> None:
    """A relabelled option would show one universe's name over another's numbers."""
    for slug, name in typescript_options():
        assert UNIVERSE_BY_SLUG[slug].name == name, slug


def test_the_two_excluded_universes_are_absent() -> None:
    """`nifty-fno` and `etf` are classifications, not size bands; the API refuses both."""
    offered = {slug for slug, _ in typescript_options()}
    assert offered.isdisjoint({"nifty-fno", "etf"})
