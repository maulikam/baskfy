"""NIFTY intraday balanced-premium strangle — PAPER ONLY (V1).

No module in this package places, modifies or cancels an order, and none imports an order
path. When live execution is built (V4) it routes through app/core/gateway.py like every
other order in this system, so the untouchable-instrument guard, the overnight-option
block and the product gates apply to it automatically.
"""
from . import book, clock, fills_paper, levels, rules, selection, sizing  # noqa: F401
