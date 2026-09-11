"""The sleeves this run must not have touched, and the one number it must share with a neighbour.

Two different claims, and both are about absence:

* ``docs/twt/04`` §1.4 — **the universe is identical to ``docs/vbt/04`` §1**, and this file is what
  makes that a fact rather than an intention. Two sleeves drawing from different universes would
  make ``01`` §6's "zero shared trades" a statement about populations rather than about events.
* ``docs/twt/02`` Track C §5 and §8 — this run writes no ``sw_`` or ``vb_`` row, changes no swing or
  VBT rule, and **reads ``baskfy_core.vbt.config`` without editing it**. A claim about what was not
  done is worth exactly as much as the test that checks it.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
from pathlib import Path

import pytest

from baskfy_core.twt.config import DEFAULT_TWT_CONFIG
from baskfy_core.twt.config import TICK_INR as TWT_TICK
from baskfy_core.vbt.config import DEFAULT_VBT_CONFIG
from baskfy_core.vbt.config import TICK_INR as VBT_TICK

CORE = Path(__file__).resolve().parents[1] / "src" / "baskfy_core"
TWT = CORE / "twt"

#: The fields ``docs/twt/04`` §1 and ``docs/vbt/04`` §1 both describe, word for word.
UNIVERSE_FIELDS = (
    "instrument_type",
    "series_allowed",
    "keep_null_series",
    "etf_universe_slug",
    "etf_name_pattern",
    "etf_symbol_pattern",
)

#: ``baskfy_core/vbt/config.py`` as it stood when the TW run began (11 Sep 2026, ``ddc7f92``).
#: ``02`` Track C §8 names this file: *"``baskfy_core.vbt.config`` is read, never edited."*
#: **Updating this constant is how a VBT change is declared.** If it moves without a VBT commit
#: beside it, something in the TW tree reached into the wrong sleeve.
VBT_CONFIG_SHA256 = "7c880c5b1d0daf88501e95d3ada8678cb5d58c1efbc8ea1c1c07c72307308b2a"


def _imports(path: Path) -> set[str]:
    """Every module this file actually imports, over the syntax tree rather than the text.

    The packages cross-reference each other in **prose** — ``twt/sizing.py`` explains how its cap
    order differs from VBT-1's, which is the kind of comment that should be encouraged. A
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


class TestTheUniverseIsTheSameUniverse:
    """``04`` §1.4."""

    @pytest.mark.parametrize("field", UNIVERSE_FIELDS)
    def test_the_two_sleeves_draw_from_the_same_names(self, field: str) -> None:
        mine = getattr(DEFAULT_TWT_CONFIG.data, field)
        theirs = getattr(DEFAULT_VBT_CONFIG.data, field)
        assert mine == theirs, (
            f"docs/twt/04 §1.4 says the universe is identical to docs/vbt/04 §1; "
            f"{field} is {mine!r} here and {theirs!r} there"
        )

    def test_the_universe_fields_are_all_of_them(self) -> None:
        """So a seventh universe field added to one sleeve and not the other is red, rather than
        silently outside the parametrisation above."""
        mine = {spec.name for spec in dataclasses.fields(DEFAULT_TWT_CONFIG.data)}
        theirs = {spec.name for spec in dataclasses.fields(DEFAULT_VBT_CONFIG.data)}
        assert set(UNIVERSE_FIELDS) <= mine & theirs

    def test_the_exchange_tick_is_one_number_for_both_books(self) -> None:
        """It is the exchange's, not a sleeve's. TWT spells its own so that ``02`` Track C §8's
        "read, never edited" holds without an import, and this is what keeps the two in step."""
        assert TWT_TICK == VBT_TICK


class TestTheCalibrationIsNotShared:
    """DECISIONS-TW **TW0.4** and ``06`` TW1: the arithmetic is shared, the numbers are not."""

    def test_the_breadth_thresholds_are_twt_s_own_fields(self) -> None:
        mine = DEFAULT_TWT_CONFIG.breadth
        assert {spec.name for spec in dataclasses.fields(mine)} >= {
            "dma_bars",
            "min_pct_above_dma",
        }
        assert type(mine).__module__ == "baskfy_core.twt.config", (
            "the breadth thresholds must be this sleeve's own dataclass, not the neighbour's"
        )

    def test_the_calendar_thresholds_are_twt_s_own_fields(self) -> None:
        mine = DEFAULT_TWT_CONFIG.data
        assert type(mine).__module__ == "baskfy_core.twt.config"
        assert mine.thin_session_window_bars == 41

    def test_this_sleeve_has_no_trend_filters_where_the_other_has_six(self) -> None:
        """``01`` §4: the trend-and-quality combination that made VBT-1 work makes this one worse.
        Adding them here would be copying a shape instead of a finding."""
        assert not hasattr(DEFAULT_TWT_CONFIG, "trend")
        assert hasattr(DEFAULT_VBT_CONFIG, "trend")


class TestNothingReachedIntoAnotherSleeve:
    """``02`` Track C §5 and §8."""

    def test_the_vbt_config_module_is_byte_identical_to_the_runs_start(self) -> None:
        digest = hashlib.sha256((CORE / "vbt" / "config.py").read_bytes()).hexdigest()
        assert digest == VBT_CONFIG_SHA256, (
            "baskfy_core.vbt.config changed during the TW run. Track C §8 says it is read, never "
            "edited. If a VBT change was deliberate, update VBT_CONFIG_SHA256 in the same commit "
            f"and say why; if it was not, the TW tree reached into the wrong sleeve. Now: {digest}"
        )

    def test_no_swing_or_vbt_module_imports_this_sleeve(self) -> None:
        """The direction that would change another book's behaviour."""
        for sleeve in ("swing", "vbt"):
            for path in sorted((CORE / sleeve).rglob("*.py")):
                offending = {name for name in _imports(path) if name.startswith("baskfy_core.twt")}
                assert offending == set(), f"{sleeve}/{path.name} imports {offending}"

    def test_the_twt_package_writes_no_neighbours_row(self) -> None:
        """A table prefix in this package is the first sign of a sleeve reaching sideways. It is
        pure code with no database at all (law 1), so any of these strings would be a mistake even
        before it was a Track C violation."""
        for path in sorted(TWT.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            for prefix in ('"sw_', '"vb_', "'sw_", "'vb_"):
                assert prefix not in source, f"{path.name} names {prefix}"

    def test_the_twt_package_names_no_regime(self) -> None:
        """R1-R4 are the weekly rebalancer's. This sleeve has a gate, not a regime, and the two
        vocabularies must not blur — ``02`` Track C §5 forbids changing the weekly book at all."""
        for path in sorted(TWT.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            for regime in ("R1", "R2", "R3", "R4"):
                assert f'"{regime}"' not in source, f"{path.name} names regime {regime}"

    @pytest.mark.parametrize("module", ["rebalance", "factors", "screener", "backtest"])
    def test_the_screener_s_own_modules_do_not_import_this_sleeve(self, module: str) -> None:
        """The product's existing surfaces knew nothing about TWT before this run and still do."""
        path = CORE / f"{module}.py"
        if not path.is_file():
            pytest.skip(f"{module}.py is not in this tree")
        offending = {name for name in _imports(path) if "twt" in name}
        assert offending == set(), f"{module}.py imports {offending}"
