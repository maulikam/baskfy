"""The exposure overlay moved to `baskfy_core.exposure` at M15 (P3.3).

Both modules were already written to core's first law — their own docstring says "no Kite calls,
no SQLite, no clock reads, no config imports… live evaluation and historical replay call the SAME
functions and, given identical inputs, produce byte-identical decisions" — so the move was a
`git mv` plus one import rewrite.

WHAT IS VERIFIED HERE, AND WHAT IS NOT.

Verified: the moved source is unchanged (`regime.py` is byte-identical to its pre-move self, zero
lines differing; `allocation.py` differs only in the import line), the algorithm version is
unchanged, and the configuration fingerprint reproduces bit-for-bit against every stored
evaluation.

**Not verified: a full replay of the persisted `regime_evaluations` rows.** M15's criterion asks
for it and no harness exists to do it — each row carries a complete `input_snapshot_json`, but
nothing reconstructs `IndexSignals`, `BreadthReading`, `BookWeights` and `ExposureSnapshot` from
that JSON and feeds them back through `evaluate()`. That harness is worth building and is recorded
as outstanding in `docs/DECISIONS-MERGE.md` M15.7 rather than quietly counted as done.
"""
from __future__ import annotations

import ast
import inspect
import pathlib
import sqlite3

import pytest

DB = pathlib.Path("data/portfolio.db")


def _stored() -> list[tuple[str, str]]:
    if not DB.is_file():
        return []
    with sqlite3.connect(DB) as conn:
        return list(
            conn.execute("SELECT DISTINCT algorithm_version, config_hash FROM regime_evaluations")
        )


def test_the_algorithm_version_still_matches_every_stored_evaluation() -> None:
    from app import config as C

    rows = _stored()
    if not rows:
        pytest.skip("no persisted evaluations here; data/ is untracked")
    cfg = C.regime_config()
    for version, _ in rows:
        assert cfg.algorithm_version == version, (
            "the exposure engine's version moved. If the logic genuinely changed, that is a new "
            "version and the stored decisions were made by a different algorithm."
        )


def test_the_config_fingerprint_reproduces_against_every_stored_evaluation() -> None:
    """The hash walks the whole RegimeConfig, so it is a real check that nothing shifted."""
    from app import config as C

    rows = _stored()
    if not rows:
        pytest.skip("no persisted evaluations here; data/ is untracked")
    cfg = C.regime_config()
    for _, stored_hash in rows:
        assert cfg.config_hash() == stored_hash


@pytest.mark.parametrize("module_name", ["regime", "allocation"])
def test_the_exposure_modules_touch_nothing(module_name: str) -> None:
    """core's first law, asserted on the moved files rather than trusted from their docstring."""
    import importlib

    module = importlib.import_module(f"baskfy_core.exposure.{module_name}")
    tree = ast.parse(pathlib.Path(inspect.getsourcefile(module)).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])

    forbidden = {"os", "socket", "sqlite3", "requests", "httpx", "httpx2", "kiteconnect", "pathlib"}
    assert not (imported & forbidden), (
        f"baskfy_core.exposure.{module_name} imports {sorted(imported & forbidden)}; core touches "
        f"nothing (docs/04 §2)."
    )


def test_the_desk_still_reaches_them_by_their_old_names() -> None:
    """Ten modules import `..core.regime`; the shims are why none of them had to move."""
    from app.core import regime, regime_alloc

    assert regime.RegimeTier.R1.value == "R1"
    assert hasattr(regime_alloc, "Allocation")
