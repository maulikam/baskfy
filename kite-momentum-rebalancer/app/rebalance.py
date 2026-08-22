"""The desk's boundary onto :mod:`baskfy_core.basket` (moved at M15, P3.2).

Plan construction itself lives in core now, where it is pure: the strategy constants, the sector
mapping and the untouchable-instrument guard all arrive as arguments. What stayed here is the part
that touches the world -- reading `data/sectors.csv` -- and the binding that supplies core with the
desk's own configuration and, crucially, the real guard.

`build_plan`'s signature is unchanged for callers, so `main.py` and `strategies/momentum_weekly.py`
did not move.
"""
from __future__ import annotations

import os

import pandas as pd
from baskfy_core import basket as _core

from . import config as C

SECTORS_FILE = "data/sectors.csv"   # optional: symbol,cluster


def _load_clusters() -> dict:
    """symbol -> cluster, or empty. The one thing here that reads the disk."""
    if os.path.exists(SECTORS_FILE):
        s = pd.read_csv(SECTORS_FILE)
        return dict(zip(s.symbol, s.cluster))
    return {}


def _tradeable(sym: str) -> bool:
    """The real guard, prefix- and series-aware, exactly as the gateway applies it.

    core's `build_plan` takes this as a REQUIRED argument so it cannot be omitted by accident.
    This is the only implementation the desk ever passes.
    """
    from .core.guards import UntouchableInstrumentError, assert_tradeable

    try:
        assert_tradeable(sym)
        return True
    except UntouchableInstrumentError:
        return False


def _cash_pct(breadth20: float) -> float:
    """Kept as the desk's name for it; M14 repoints the breadth input, not this band table."""
    return _core.cash_pct_for(breadth20, C)


def build_plan(scored: pd.DataFrame, holdings: list[dict], cash: float,
               live_prices: dict[str, float] | None = None) -> dict:
    """Same signature as before the move; binds the desk's config, clusters and guard."""
    return _core.build_plan(
        scored,
        holdings,
        cash,
        cfg=C,
        tradeable=_tradeable,
        clusters=_load_clusters(),
        live_prices=live_prices,
    )
