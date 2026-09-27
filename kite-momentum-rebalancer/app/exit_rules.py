"""The exact exit rule each sleeve runs, said from its config (LV6 — review P2.3).

One sentence per sleeve, built from ``DEFAULT_*_CONFIG`` so the trade card cannot drift from
the code that manages the position. No targets: TWT and VBT measured fixed targets and rejected
them; the swing book takes a partial and trails. Nothing here decides anything.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from baskfy_core.swing.config import DEFAULT_SWING_CONFIG
from baskfy_core.twt.config import DEFAULT_TWT_CONFIG
from baskfy_core.vbt.config import DEFAULT_VBT_CONFIG


def _pct(value: object) -> str:
    """``20.0`` and ``Decimal("20.0")`` both read ``20``; ``12.5`` stays ``12.5``."""
    return f"{Decimal(str(value)).normalize():f}"


def _swing_rule() -> str:
    s = DEFAULT_SWING_CONFIG.stops
    return (
        f"stop under the range low, at most {_pct(s.max_stop_distance_pct)}% below entry; "
        f"{s.partial_numerator}/{s.partial_denominator} sold between bar {s.partial_earliest_bar} "
        f"and {s.partial_latest_bar}, then the stop to breakeven (or after {_pct(s.breakeven_after_r)}R); "
        f"the rest trails the 10-day MA (ADR at or above {_pct(s.fast_trail_min_adr_pct)}%) or the "
        f"20-day MA; the GTT is the exit"
    )


EXIT_RULES: Final[dict[str, str]] = {
    "twt": (
        f"{_pct(DEFAULT_TWT_CONFIG.exits.stop_pct)}% initial stop under the fill; a "
        f"{_pct(DEFAULT_TWT_CONFIG.exits.trail_pct)}% high-water trail ratchets the GTT each evening; "
        f"no target, no time stop — the GTT is the only exit"
    ),
    "vbt": (
        f"{_pct(DEFAULT_VBT_CONFIG.exits.stop_pct)}% initial stop under the signal close (GTT); the "
        f"working exit is a close below the 21-day EMA, sold at the next open; no target"
    ),
    "swing": _swing_rule(),
}
