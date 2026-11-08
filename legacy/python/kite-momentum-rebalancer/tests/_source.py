"""Read a module's source by MODULE, never by path.

Several of these tests assert structural properties by scanning source — the gateway's four
layers appear in order, every status the gateway can return is explained on the report, no
scoring formula has crept back into a wrapper. Those are good tests: they catch things a
behavioural test cannot, like a refactor that quietly reorders the guard and the risk check.

They were written as `open("app/core/gateway.py")`, and that path is a coupling. The merge moved
four modules into `packages/` and every one of those reads broke — not because the property
stopped holding, but because the file moved. Three separate times, in three separate modules.

So: ask the module where it lives. `src_of(module)` follows the code, and the next move will not
break it either.
"""
from __future__ import annotations

import inspect
import pathlib
from types import ModuleType


def src_of(module: ModuleType) -> str:
    """The module's own source text, wherever the module happens to live."""
    path = inspect.getsourcefile(module)
    if path is None:                                    # pragma: no cover - C ext, never us
        raise RuntimeError(f"{module.__name__} has no source file")
    return pathlib.Path(path).read_text()
