"""Is the frozen options lab present? One answer for every surface that would show it (OP13).

The strangle lab has been frozen in `frozen/strangle/` since M6. Four surfaces of the live tree
would wake if `OPTIONS_ENABLED` were set: the `/options*` routes, their nav entry, the five `/ops`
controls and the autorun's options loop (docs/options/STATUS "What `OPTIONS_ENABLED=true` would
wake today"). They used to be gated on the flag plus an `ImportError` from
`strategies.strangle.instruments` — one of the four strangle modules M6 left live, so the import
succeeded and `/ops` showed five controls that spawn a frozen script (DECISIONS-OP OP0.5).

The options book (`app/options_*`, `/nifty-options`) needs `OPTIONS_ENABLED` for a live leg, so
the flag alone can no longer mean "the lab". `lab_enabled()` is the flag **and** the lab's own
code: a *frozen* module (`strategies.strangle.book`) and its runner (`scripts/strangle.py`) —
the same probe `tests/_frozen.py` uses. Thawing the lab is still a `git mv` and nothing else.
`frozen/` is not touched (DECISIONS-OP OP13.1).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from . import config as C

ROOT = Path(__file__).resolve().parents[1]


def lab_present() -> bool:
    """Both halves of the lab are in the live tree: the package and the runnable script."""
    if importlib.util.find_spec("app.strategies.strangle.book") is None:
        return False
    return (ROOT / "scripts" / "strangle.py").exists()


def lab_enabled() -> bool:
    """`OPTIONS_ENABLED` and the lab thawed: the only state in which its surfaces appear."""
    return bool(C.OPTIONS_ENABLED) and lab_present()
