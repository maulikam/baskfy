"""The options goldens (OP15) still say what the live code says, byte for byte.

``go/testdata/golden/L1/options/*.json`` is the Go lane's answer key for six pure functions of
``baskfy_core.options``, dumped by ``tools/parity/golden_options.py`` from inputs written in full.
This recomputes every file from its stored inputs through the live function and re-renders it with
the dumper's own encoder; a rule change fails here first. It also checks that the dumper's case set
is exactly what is on disk, so a case added in code but not dumped (or the reverse) fails too.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[4]
DUMPER = ROOT / "tools" / "parity" / "golden_options.py"
LANE_DIR = ROOT / "go" / "testdata" / "golden" / "L1" / "options"
CASES = sorted(LANE_DIR.glob("*.json")) if LANE_DIR.is_dir() else []


def _dumper() -> ModuleType:
    name = "baskfy_parity_golden_options"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, DUMPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_the_lane_on_disk_is_the_dumpers_case_set() -> None:
    dumper = _dumper()
    expected = {f"{stem}.json" for _fn, stem, _inputs in dumper.cases()}
    assert {p.name for p in CASES} == expected
    assert {fn for fn, _s, _i in dumper.cases()} == set(dumper.CALLS)


@pytest.mark.parametrize("path", CASES, ids=lambda p: p.stem)
def test_each_golden_is_recomputed_byte_for_byte(path: Path) -> None:
    dumper = _dumper()
    text = path.read_text(encoding="utf-8")
    doc = json.loads(text)
    assert dumper.render_case(doc["fn"], doc["inputs"]) == text
