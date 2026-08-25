"""The web's weight-method table is the same table as core's (SB4).

``apps/web/src/lib/basket/methods.ts`` duplicates :class:`baskfy_core.basket_sizing.WeightMethod`
for the same reason ``profiles.ts`` duplicates the holding-profile table: no ``/meta/`` endpoint
publishes it, and a round trip per keystroke would be a worse trade than this test.

The failure this prevents is a preview that splits the money one way while the save endpoint
writes another, with the investor never told which one they got.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from baskfy_core.basket_sizing import DEFAULT_METHOD, WeightMethod

MONOREPO_ROOT = Path(__file__).resolve().parents[3]
METHODS_TS = MONOREPO_ROOT / "apps" / "web" / "src" / "lib" / "basket" / "methods.ts"


@pytest.fixture(scope="module")
def source() -> str:
    assert METHODS_TS.is_file(), f"{METHODS_TS} is the mirror this test exists to check"
    return METHODS_TS.read_text(encoding="utf-8")


def _record(source: str, name: str) -> dict[str, str]:
    match = re.search(rf"{name}[^=]*=\s*\{{(.*?)\n\}};", source, re.DOTALL)
    assert match is not None, f"{name} is missing from methods.ts"
    return {key: value.strip() for key, value in re.findall(r"(\w+):\s*([^,\n]+),", match.group(1))}


def test_the_method_names_match(source: str) -> None:
    listed = re.search(r"WEIGHT_METHODS = \[(.*?)\] as const;", source, re.DOTALL)
    assert listed is not None
    names = set(re.findall(r'"(\w+)"', listed.group(1)))
    assert names == {method.value for method in WeightMethod}


def test_the_default_method_is_equal(source: str) -> None:
    match = re.search(r'DEFAULT_METHOD: WeightMethod = "(\w+)"', source)
    assert match is not None
    assert match.group(1) == DEFAULT_METHOD.value
    assert DEFAULT_METHOD is WeightMethod.EQUAL


def test_every_method_has_a_label_a_blurb_and_a_thesis(source: str) -> None:
    expected = {method.value for method in WeightMethod}
    for record in ("METHOD_LABELS", "METHOD_BLURBS", "METHOD_THESES"):
        assert set(_record(source, record)) == expected
