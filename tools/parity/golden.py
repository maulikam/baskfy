"""Canonical JSON dumper for Go parity goldens (docs/go-rewrite/03-parity-and-gates.md).

Import from a lane's dumper, run with the tree's own venv:

    from tools.parity.golden import dump
    dump("L1/score/case_001", fn="baskfy_core.score.score_frame",
         inputs={"frame": df, "asof": date(2026, 8, 22)}, output=score_frame(df, asof),
         known_bug=None, tolerance={"float": 1e-9})

Writes go/testdata/golden/<case>.json. Never modifies Python, never touches the network.
Encoding rules (mirrored by go/internal/testkit/golden.go):
  Decimal -> string ("12.34")        date/datetime -> ISO 8601 (datetimes carry the offset)
  NaN/inf -> null                    set/frozenset -> sorted list
  pandas.DataFrame -> {"columns": [...], "dtypes": {...}, "rows": [[...], ...]}
  pandas.Series    -> {"index": [...], "values": [...]}
  polars.DataFrame -> same shape as pandas (via to_pandas() when available, else to_dicts())
  numpy scalars/arrays -> python scalars/lists       dataclass/pydantic -> dict
Key order is sorted; floats are written with repr precision so 1e-9 comparisons are meaningful.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import math
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = ROOT / "go" / "testdata" / "golden"


def _enc(o: Any) -> Any:  # noqa: PLR0911 - one branch per type is the point
    if o is None or isinstance(o, (bool, int, str)):
        return o
    if isinstance(o, float):
        return None if (math.isnan(o) or math.isinf(o)) else o
    if isinstance(o, Decimal):
        return None if o.is_nan() else format(o, "f")
    if isinstance(o, _dt.datetime):
        return o.isoformat()
    if isinstance(o, _dt.date):
        return o.isoformat()
    if isinstance(o, (set, frozenset)):
        return sorted((_enc(x) for x in o), key=json.dumps)
    if isinstance(o, dict):
        return {str(k): _enc(v) for k, v in sorted(o.items(), key=lambda kv: str(kv[0]))}
    if isinstance(o, (list, tuple)):
        return [_enc(x) for x in o]
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return _enc(dataclasses.asdict(o))
    if hasattr(o, "model_dump"):  # pydantic v2
        return _enc(o.model_dump())
    try:  # numpy without importing numpy
        import numpy as np  # type: ignore[import-not-found]

        if isinstance(o, np.generic):
            return _enc(o.item())
        if isinstance(o, np.ndarray):
            return _enc(o.tolist())
    except ImportError:
        pass
    try:
        import pandas as pd  # type: ignore[import-not-found]

        if isinstance(o, pd.DataFrame):
            return {
                "columns": [str(c) for c in o.columns],
                "dtypes": {str(c): str(t) for c, t in o.dtypes.items()},
                "index": _enc(list(o.index)),
                "rows": [_enc(list(r)) for r in o.itertuples(index=False, name=None)],
            }
        if isinstance(o, pd.Series):
            return {"index": _enc(list(o.index)), "values": _enc(list(o.values))}
        if isinstance(o, pd.Timestamp):
            return o.isoformat()
    except ImportError:
        pass
    try:
        import polars as pl  # type: ignore[import-not-found]

        if isinstance(o, pl.DataFrame):
            try:
                return _enc(o.to_pandas())
            except Exception:  # pyarrow absent
                return {"columns": o.columns, "dtypes": {c: str(t) for c, t in o.schema.items()},
                        "rows": [_enc(list(r.values())) for r in o.to_dicts()]}
        if isinstance(o, pl.Series):
            return {"values": _enc(o.to_list())}
    except ImportError:
        pass
    if hasattr(o, "__dict__"):
        return _enc(vars(o))
    raise TypeError(f"golden: cannot encode {type(o).__name__}")


def dump(case: str, *, fn: str, inputs: Any, output: Any,
         known_bug: str | None = None, tolerance: dict[str, float] | None = None,
         notes: str | None = None) -> Path:
    """Write one golden case; returns its path. `case` is '<lane>/<module>/<name>'."""
    path = GOLDEN_DIR / f"{case}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "fn": fn,
        "inputs": _enc(inputs),
        "output": _enc(output),
        "meta": {
            "dumped_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "python": sys.version.split()[0],
            "cwd": os.path.relpath(os.getcwd(), ROOT),
            "known_bug": known_bug,
            "tolerance": tolerance or {"float": 1e-9},
            "notes": notes,
        },
    }
    path.write_text(json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    return path


if __name__ == "__main__":
    print(f"golden dir: {GOLDEN_DIR}")
