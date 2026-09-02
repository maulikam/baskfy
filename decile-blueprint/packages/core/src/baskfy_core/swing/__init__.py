"""``baskfy_core.swing`` — the Kullamägi-style swing setups, as pure functions.

Specification: ``docs/swing/`` at the repo root (``01-method.md`` is the method,
``04-business-rules.md`` is the numerical contract every test here asserts). The package obeys
law 1 of ``CLAUDE.md``: DataFrames and dataclasses in, DataFrames and dataclasses out — no
database, no network, no disk, no clock. The worker, the API and the desk own every read,
write and timestamp.

Three setups, one vocabulary::

    FLAG              a leader that ran, then rested — the breakout / continuation setup
    EP                an episodic pivot: a catalyst gap out of a neglected base
    PARABOLIC_SHORT   an exhausted runner — DETECTED ONLY, never traded on NSE delivery

Module map
----------
``config``          every threshold, as a frozen dataclass with the method's defaults
``indicators``      per-bar columns the detectors read (ADR%, turnover, MAs, streaks, gaps)
``setups``          the three detectors, one as-of date, one row per candidate
``sizing``          risk-per-trade → share count, with every cap named
``stops``           the initial stop, the trail MA choice, and the day-by-day management rules
``market``          breadth, the market gate, and progressive exposure from the trader's results
``opening_range``   ORH/ORL from intraday candles, and the trigger verdict for the live monitor
``plan``            a day's watchlist + account → a plan the desk can confirm line by line
``journal``         R-multiples and the statistics progressive exposure reads
"""

from __future__ import annotations

from baskfy_core.swing.config import (
    DEFAULT_SWING_CONFIG,
    EpConfig,
    FlagConfig,
    LiquidityConfig,
    MarketConfig,
    OpeningRangeConfig,
    ParabolicConfig,
    Setup,
    SizingConfig,
    StopConfig,
    SwingConfig,
)

__all__ = [
    "DEFAULT_SWING_CONFIG",
    "EpConfig",
    "FlagConfig",
    "LiquidityConfig",
    "MarketConfig",
    "OpeningRangeConfig",
    "ParabolicConfig",
    "Setup",
    "SizingConfig",
    "StopConfig",
    "SwingConfig",
]
