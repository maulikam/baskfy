"""The web's holding-profile table is the same table as core's (SB1).

``apps/web/src/lib/basket/profiles.ts`` duplicates :mod:`baskfy_core.basket_sizing` because no
``/meta/`` endpoint publishes it and a round trip per keystroke would be a worse trade than this
test. The same arrangement, and the same reason, as ``test_operand_parity.py`` and
``test_market_health_universe_parity.py``.

The failure this prevents is quiet and expensive: a preview that suggests twenty names while the
save endpoint writes twelve, with the investor never told which one they got.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest

from baskfy_core.basket_sizing import (
    DEFAULT_PROFILE,
    MAX_CASH_PCT,
    MAX_HOLDINGS,
    MIN_CASH_BUFFER_PCT,
    MIN_HOLDINGS,
    SUGGESTED_HOLDINGS,
    HoldingProfile,
    cash_pct_for_tier,
)
from baskfy_core.sleeves import cap_for_tier

MONOREPO_ROOT = Path(__file__).resolve().parents[3]
PROFILES_TS = MONOREPO_ROOT / "apps" / "web" / "src" / "lib" / "basket" / "profiles.ts"


@pytest.fixture(scope="module")
def source() -> str:
    assert PROFILES_TS.is_file(), f"{PROFILES_TS} is the mirror this test exists to check"
    return PROFILES_TS.read_text(encoding="utf-8")


def _record(source: str, name: str) -> dict[str, str]:
    """Parse a `Record<...> = { KEY: value, ... }` literal out of the TypeScript."""
    match = re.search(rf"{name}[^=]*=\s*\{{(.*?)\n\}};", source, re.DOTALL)
    assert match is not None, f"{name} is missing from profiles.ts"
    return {key: value.strip() for key, value in re.findall(r"(\w+):\s*([^,\n]+),", match.group(1))}


def _number(source: str, name: str) -> str:
    match = re.search(rf"export const {name} = ([\d.]+);", source)
    assert match is not None, f"{name} is missing from profiles.ts"
    return match.group(1)


def test_the_profile_names_match(source: str) -> None:
    listed = re.search(r"HOLDING_PROFILES = \[(.*?)\] as const;", source, re.DOTALL)
    assert listed is not None
    names = set(re.findall(r'"(\w+)"', listed.group(1)))
    assert names == {profile.value for profile in HoldingProfile}


def test_the_suggested_counts_match(source: str) -> None:
    mirrored = {key: int(value) for key, value in _record(source, "SUGGESTED_HOLDINGS").items()}
    assert mirrored == {profile.value: count for profile, count in SUGGESTED_HOLDINGS.items()}


def test_the_default_profile_matches(source: str) -> None:
    match = re.search(r'DEFAULT_PROFILE: HoldingProfile = "(\w+)"', source)
    assert match is not None
    assert match.group(1) == DEFAULT_PROFILE.value


@pytest.mark.parametrize(
    ("constant", "expected"),
    [
        ("MIN_HOLDINGS", MIN_HOLDINGS),
        ("MAX_HOLDINGS", MAX_HOLDINGS),
        ("MIN_CASH_BUFFER_PCT", MIN_CASH_BUFFER_PCT),
        ("MAX_CASH_PCT", MAX_CASH_PCT),
        ("ZERO_CASH_PCT", 0),
    ],
)
def test_the_bounds_match(source: str, constant: str, expected: int | Decimal) -> None:
    assert Decimal(_number(source, constant)) == Decimal(expected)


def test_the_exposure_caps_match_the_desks_table(source: str) -> None:
    """The cash share is derived from these, so a drift here silently changes every basket."""
    caps = _record(source, "TIER_EQUITY_CAP_PCT")
    mirrored = {key: Decimal(value) for key, value in caps.items()}
    assert mirrored == {tier: cap_for_tier(tier) for tier in ("R1", "R2", "R3", "R4")}


@pytest.mark.parametrize("tier", ["R1", "R2", "R3", "R4"])
def test_every_tier_yields_the_same_cash_share_on_both_sides(source: str, tier: str) -> None:
    """Recomputes the TypeScript's formula from its own mirrored constants."""
    caps = {key: Decimal(value) for key, value in _record(source, "TIER_EQUITY_CAP_PCT").items()}
    floor = Decimal(_number(source, "MIN_CASH_BUFFER_PCT"))
    ceiling = Decimal(_number(source, "MAX_CASH_PCT"))
    mirrored = min(ceiling, max(floor, Decimal(100) - caps[tier]))
    assert mirrored == cash_pct_for_tier(tier)


def test_every_profile_has_a_label_and_a_blurb(source: str) -> None:
    """Copy, not arithmetic — but a profile the UI cannot name is a profile nobody can pick."""
    for record in ("PROFILE_LABELS", "PROFILE_BLURBS"):
        assert set(_record(source, record)) == {profile.value for profile in HoldingProfile}
