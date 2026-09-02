"""The swing goldens (SW12, STANDING-ANSWERS B11) still say what the live code says.

``go/testdata/golden/L1/swing/*.json`` is the answer key the Go lane is graded against, dumped
by ``tools/parity/golden.py swing`` from the fixture set in ``swing_fixtures`` and the swing
test modules. Every case stores its inputs in full; this test feeds those inputs back through
the live function and re-renders the whole file with the dumper's own encoder. A rule change in
``baskfy_core.swing`` therefore fails **here first**, byte for byte, before any Go test reads a
golden that no longer describes the Python it was meant to mirror. Regenerating is one command
and one commit that says why (docs/go-rewrite/03-parity-and-gates.md).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[4]
GOLDEN_PY = ROOT / "tools" / "parity" / "golden.py"
SWING_DIR = ROOT / "go" / "testdata" / "golden" / "L1" / "swing"

#: B11 names these six. Each must have at least one case on disk.
SIX_FUNCTIONS = (
    "baskfy_core.swing.setups.detect_setups",
    "baskfy_core.swing.sizing.size_position",
    "baskfy_core.swing.stops.manage",
    "baskfy_core.swing.market.exposure_tier",
    "baskfy_core.swing.plan.build_entries",
    "baskfy_core.swing.opening_range.evaluate_trigger",
)

CASES = sorted(SWING_DIR.glob("*.json")) if SWING_DIR.is_dir() else []


def _dumper() -> ModuleType:
    """``tools/parity/golden.py`` sits at the repo root, outside every package; load it by path."""
    name = "baskfy_parity_golden"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, GOLDEN_PY)
    assert spec is not None and spec.loader is not None, GOLDEN_PY
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: its dataclasses resolve their annotations through
    # ``sys.modules[__module__]`` (the ``from __future__ import annotations`` recipe).
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _doc(path: Path) -> dict[str, object]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), path
    return loaded


def test_the_lane_is_on_disk_and_covers_all_six_functions() -> None:
    assert GOLDEN_PY.is_file(), "the dumper is gone"
    assert len(CASES) >= len(SIX_FUNCTIONS), f"{len(CASES)} goldens under {SWING_DIR}"
    on_disk = {_doc(path)["fn"] for path in CASES}
    assert set(SIX_FUNCTIONS) <= on_disk, sorted(set(SIX_FUNCTIONS) - on_disk)
    assert on_disk <= set(_dumper().SWING_FUNCTIONS), "a golden names a function the lane lacks"


@pytest.mark.parametrize("path", CASES, ids=[path.stem for path in CASES])
def test_each_golden_recomputes_byte_for_byte(path: Path) -> None:
    """Stored inputs → the live function → the dumper's encoder → the file, unchanged."""
    golden = _dumper()
    text = path.read_text(encoding="utf-8")
    doc = _doc(path)
    assert doc["fn"] in SIX_FUNCTIONS, doc["fn"]
    meta = doc["meta"]
    assert isinstance(meta, dict) and meta.get("stable") is True, "swing goldens are byte-stable"
    recomputed = golden.swing_recompute(doc)
    rebuilt = {
        "fn": doc["fn"],
        "inputs": doc["inputs"],
        "output": golden._enc(recomputed),
        "meta": meta,
    }
    assert golden.render(rebuilt) == text, (
        f"{path.name} drifted from the live function; if the rule changed on purpose, run "
        "`uv run python ../tools/parity/golden.py swing` and commit the regenerated goldens "
        "with the reason"
    )
