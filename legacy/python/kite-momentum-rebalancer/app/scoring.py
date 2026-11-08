"""The desk's boundary onto :mod:`baskfy_core.score` (moved at M15, P3.1).

The Momentum Quality Score itself lives in core now, where it is pure and configuration arrives
as an argument. Two things stayed here, and both are boundaries in the sense docs/04 §2 means:

* :func:`load_scan` reads a CSV from disk;
* the ``__main__`` block reads ``sys.argv`` and prints.

Everything else is a thin binding of the desk's own ``config`` module to core's functions, so that
every existing ``from .scoring import score, audit, load_scan, stop_from_vol`` keeps working and
the move stays a move.
"""
from __future__ import annotations

import sys

import pandas as pd
from baskfy_core import score as _core

from . import config as C

#: Kept as module attributes because the desk's own code and tests read them.
SHP_COLS = _core.SHP_COLS
RET_COLS = _core.ret_cols(C)
REQUIRED = _core.required(C)


def load_scan(path: str) -> pd.DataFrame:
    """Read a weekly scan CSV and check it carries everything the score needs.

    The one function here that touches the world, which is why it did not move.
    """
    u = pd.read_csv(path, encoding="utf-8-sig")
    missing = [c for c in REQUIRED if c not in u.columns]
    if missing:
        raise ValueError(f"Scan CSV missing columns: {missing}")
    u = u.drop_duplicates(subset="symbol").reset_index(drop=True)
    return u


def audit(u: pd.DataFrame) -> dict:
    return _core.audit(u, C)


def apply_filters(u: pd.DataFrame) -> pd.DataFrame:
    return _core.apply_filters(u, C)


def score(u: pd.DataFrame) -> pd.DataFrame:
    return _core.score(u, C)


def stop_from_vol(price: float, ann_vol: float) -> float:
    return _core.stop_from_vol(price, ann_vol, C)


if __name__ == "__main__":
    df = score(load_scan(sys.argv[1]))
    cols = ["rank", "symbol", "SCORE", "close", "rsi_one_month",
            "away_from_high_one_year", "ext_over_20dma", "reject"]
    print(df.sort_values("rank").head(30)[cols].to_string(index=False))
