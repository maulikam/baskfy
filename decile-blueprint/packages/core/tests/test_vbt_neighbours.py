"""VB10: the two sleeves this run must not have touched, and the desk that must still rebalance.

The run's own charter: *"Track C is forbidden: … no changes to the swing rules or the weekly
book"* and *"the desk must be able to rebalance on any Friday."* Those are claims about what was
**not** done, and a claim about absence is worth exactly as much as the test that checks it.

`baskfy_core.swing` is hashed rather than diffed, because the useful assertion is "no byte of the
swing rules moved", and a hash is the cheapest true statement of that. The number below is the
tree's state at the VB run's start; a legitimate swing change updates it in the same commit that
makes the change, which is the point — it becomes a visible act rather than a silent one.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parents[1] / "src" / "baskfy_core"
MONOREPO = Path(__file__).resolve().parents[4]


def _imports(path: Path) -> set[str]:
    """Every module this file actually imports.

    Over the syntax tree rather than the text, because both packages cross-reference each other
    in **prose** — `vbt/indicators.py` explains how its rolling windows differ from the swing
    book's, which is exactly the kind of comment that should be encouraged. A cross-reference is
    not a dependency; an import is.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def _tree_hash(root: Path) -> str:
    """A stable digest of every ``.py`` under ``root``, by path and content."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


#: `packages/core/src/baskfy_core/swing/` as it stood when the VB run began (10 Sep 2026).
#: Verified to be the start state rather than merely the current one:
#: `git log c68c57f..HEAD -- packages/core/src/baskfy_core/swing` is empty, so nothing has
#: touched the swing package since the commit this run branched from.
#: **Updating this constant is how a swing change is declared.** If it moves without a swing
#: commit beside it, something in the VB tree reached into the wrong sleeve.
#: **Moved 12 Sep 2026, deliberately, and declared here as this constant asks.**
#: `swing/config.py` and `swing/sizing.py` changed `min_trade_value_inr` from `float` to
#: `Decimal` under CLAUDE.md house rule 9 ("money and prices are `numeric`, never `float`")
#: — the same edit was made to the VBT sleeve in the same commit, and the TWT sleeve has
#: always been correct. This is not the VB tree reaching into the swing sleeve: it is one
#: house rule applied to both sleeves at once, with the value and behaviour unchanged
#: (`Decimal("10000")` compares identically to the `Decimal(str(10_000.0))` the call site
#: was building anyway). See `gates/pinned-wrong-behaviour.md` P3.
SWING_TREE_SHA256 = "1169ea2b69eddcdcc2e4bdb783339d729bd29decc00536652fea244bcb1ea734"


class TestTheSwingRulesDidNotMove:
    def test_the_swing_package_is_byte_identical_to_the_runs_start(self) -> None:
        actual = _tree_hash(CORE / "swing")
        assert actual == SWING_TREE_SHA256, (
            "baskfy_core.swing changed during the VB run. If that was deliberate, update "
            "SWING_TREE_SHA256 in the same commit and say why; if it was not, the VB tree "
            f"reached into the swing sleeve. Now: {actual}"
        )

    def test_no_vbt_module_imports_the_swing_package(self) -> None:
        """Two sleeves that share a rule are one sleeve with two names."""
        for path in sorted((CORE / "vbt").rglob("*.py")):
            offending = {name for name in _imports(path) if name.startswith("baskfy_core.swing")}
            assert offending == set(), f"{path.name} imports {offending}"

    def test_no_swing_module_imports_the_vbt_package(self) -> None:
        """And the reverse, which is the direction that would change the swing book's behaviour."""
        for path in sorted((CORE / "swing").rglob("*.py")):
            offending = {name for name in _imports(path) if name.startswith("baskfy_core.vbt")}
            assert offending == set(), f"{path.name} imports {offending}"


class TestTheWeeklyBookAndItsRegimes:
    def test_the_vbt_package_names_no_regime(self) -> None:
        """R1-R4 are the weekly rebalancer's. This sleeve has a gate, not a regime, and the two
        vocabularies must not blur — `02` Track C forbids changing the weekly book at all."""
        for path in sorted((CORE / "vbt").rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            for regime in ("R1", "R2", "R3", "R4"):
                assert f'"{regime}"' not in source, f"{path.name} names regime {regime}"

    @pytest.mark.parametrize("module", ["rebalance", "factors", "screener", "backtest"])
    def test_the_screener_s_own_modules_do_not_import_this_sleeve(self, module: str) -> None:
        """The product's existing surfaces knew nothing about VBT before this run and still do."""
        path = CORE / f"{module}.py"
        if not path.is_file():
            pytest.skip(f"{module}.py is not in this tree")
        offending = {name for name in _imports(path) if "vbt" in name}
        assert offending == set(), f"{module}.py imports {offending}"
