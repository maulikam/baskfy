"""The exact exit rule each sleeve runs, said from its config (LV6 — review P2.3).

One sentence per sleeve, built from ``DEFAULT_*_CONFIG`` so the trade card cannot drift from
the code that manages the position. No targets anywhere. Since LV9 (Maulik, 28 Sep 2026) all
three sleeves run Qullamaggie's exits — the partial into strength, breakeven, the 10/20-day MA
trail — with each sleeve's own hard stop under it. Nothing here decides anything.
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


def _qulla_rule(prefix: str, s: object) -> str:
    """LV9 (Maulik, 28 Sep 2026 — DECISIONS-LV LV9.0): Qullamaggie's exits, the swing book's
    ``StopConfig``, on TWT and VBT too. ``prefix`` is the sleeve's own hard stop."""
    return (
        f"{prefix}; "
        f"{s.partial_numerator}/{s.partial_denominator} sold into strength between bar "
        f"{s.partial_earliest_bar} and bar {s.partial_latest_bar} after entry if green, then the "
        f"stop to breakeven (or after {_pct(s.breakeven_after_r)}R); the rest trails the 10-day MA "
        f"(ADR at or above {_pct(s.fast_trail_min_adr_pct)}%) or the 20-day MA and is sold at the "
        f"next open on a close below it; the GTT is the hard stop (Qullamaggie's rule)"
    )


def _twt_rule() -> str:
    e = DEFAULT_TWT_CONFIG.exits
    if e.qulla_exits:
        return _qulla_rule(f"{_pct(e.stop_pct)}% initial stop under the fill (GTT)", e.qulla)
    return (
        f"{_pct(e.stop_pct)}% initial stop under the fill; a {_pct(e.trail_pct)}% high-water "
        f"trail ratchets the GTT each evening; no target, no time stop — the GTT is the only exit"
    )


def _vbt_rule() -> str:
    e = DEFAULT_VBT_CONFIG.exits
    if e.qulla_exits:
        return _qulla_rule(f"{_pct(e.stop_pct)}% initial stop under the signal close (GTT)", e.qulla)
    return (
        f"{_pct(e.stop_pct)}% initial stop under the signal close (GTT); the working exit is a "
        f"close below the 21-day EMA, sold at the next open; no target"
    )


EXIT_RULES: Final[dict[str, str]] = {
    "twt": _twt_rule(),
    "vbt": _vbt_rule(),
    "swing": _swing_rule(),
}
