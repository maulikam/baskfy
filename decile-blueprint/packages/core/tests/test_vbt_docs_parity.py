"""``docs/vbt/04-business-rules.md`` is the contract; this test is what makes that true.

`04` §12 carries one row per field of ``baskfy_core.vbt.config`` with the value it holds. This
test regenerates that table from the code and asserts it **both ways**: a field added, renamed or
re-valued without a doc edit is red, and a row in the document with no field behind it is red
too. ``docs/vbt/06`` VB1 AC 3.

The document itself carries CLAUDE.md's rule at its head, and it is worth restating where a test
enforces a document: **when the code and a doc disagree, look for the commit that changed the
value before assuming the code is wrong.** A setting somebody deliberately changed, with a commit
message saying why, is a later fact than a paragraph. This test says the two are in step; it does
not say the document is the authority over a decision.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from baskfy_core.vbt.config import DEFAULT_VBT_CONFIG, TrendFilter, VbtConfig
from baskfy_core.vbt.signals import trend_filters

DOC = Path(__file__).resolve().parents[4] / "docs" / "vbt" / "04-business-rules.md"
_ROW = re.compile(r"^\| `([a-z_]+\.[a-z_0-9]+)` \| `(.*)` \|$")


def render(value: object) -> str:
    """How a default is written in §12's table. One renderer, used by the test and the doc."""
    if isinstance(value, tuple):
        return ", ".join(str(item) for item in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def from_code() -> dict[str, str]:
    out: dict[str, str] = {}
    for group in dataclasses.fields(DEFAULT_VBT_CONFIG):
        sub = getattr(DEFAULT_VBT_CONFIG, group.name)
        for spec in dataclasses.fields(sub):
            out[f"{group.name}.{spec.name}"] = render(getattr(sub, spec.name))
    return out


def from_doc() -> dict[str, str]:
    rows: dict[str, str] = {}
    for line in DOC.read_text(encoding="utf-8").splitlines():
        found = _ROW.match(line)
        if found:
            rows[found.group(1)] = found.group(2)
    return rows


def test_the_contract_document_exists() -> None:
    assert DOC.is_file(), f"the numerical contract is missing at {DOC}"


def test_the_document_names_every_field_and_nothing_else() -> None:
    code, doc = from_code(), from_doc()
    assert sorted(doc) == sorted(code), (
        f"only in the code: {sorted(set(code) - set(doc))}; "
        f"only in docs/vbt/04 §12: {sorted(set(doc) - set(code))}"
    )


@pytest.mark.parametrize(("field", "value"), sorted(from_code().items()))
def test_every_default_is_the_documented_value(field: str, value: str) -> None:
    assert from_doc().get(field) == value, f"docs/vbt/04 §12 disagrees about {field}"


def test_the_prose_states_the_load_bearing_numbers() -> None:
    """The table is machine-checked; these are the numbers a person must be able to *read*."""
    text = DOC.read_text(encoding="utf-8")
    for phrase in (
        "**3.0**",  # the volume multiple
        "**6.5**",  # the day's change
        "**₹30.0**",  # Chartink's price floor, an exchange price
        "**200**",  # the trend filter
        "[**40.0**]",  # the gate
        "[**3**]",  # the three-session window, the parameter with a cliff
        "[**12.0**]",  # the disaster stop
        "[**21**]",  # the working exit
        "[**10**]",  # the slots
        "[**0.25**]",  # the cost a side
    ):
        assert phrase in text, f"docs/vbt/04's prose no longer states {phrase}"


def test_there_are_exactly_six_trend_filters() -> None:
    """DECISIONS-VB VB0.2: the 50-DMA and the ADR ceiling ``grid2.py`` still applies are **not**
    fields here. A field that exists is a field somebody turns on."""
    assert len(TrendFilter) == 6
    assert set(trend_filters()) == set(TrendFilter)
    fields = {name.split(".")[1] for name in from_code()}
    assert not {"ma_trend", "max_adr_pct", "adr_bars", "sma_fast_bars"} & fields


def test_no_target_partial_or_time_stop_field_exists() -> None:
    """``04`` §6.3 — each was tested and each lowered the result, so none of them is a knob."""
    fields = {name.split(".")[1] for name in from_code()}
    forbidden = {f for f in fields if f.startswith(("target_", "partial_")) or "max_hold" in f}
    assert forbidden == set()


def test_the_config_dataclasses_are_frozen() -> None:
    """A threshold that can be mutated at runtime is a threshold nobody can reconstruct later."""
    for group in dataclasses.fields(VbtConfig()):
        sub = getattr(VbtConfig(), group.name)
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(sub, dataclasses.fields(sub)[0].name, None)
