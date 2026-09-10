"""``baskfy_core.vbt`` — VBT-1, the volume-breakout strategy, as pure functions.

Specification: ``docs/vbt/`` at the repo root (``01-method.md`` is the method, taken from
``research/volume-breakout/STRATEGY.md``; ``04-business-rules.md`` is the numerical contract every
test here asserts). The package obeys law 1 of ``CLAUDE.md``: DataFrames and dataclasses in,
DataFrames and dataclasses out — no database, no network, no disk, no clock. The worker, the API
and the desk own every read, write and timestamp.

One sentence of the strategy: a 6.5%+ day on three times its 50-day volume, **in an established
uptrend, in a liquid name, with a controlled bar**, bid at that day's close for three sessions,
stopped 12% below the fill and sold at the next open on the first close under the 21-day EMA —
and no new entry at all unless more than 40% of the traded universe is above its own 200-day
average.

Module map
----------
``config``      every threshold, as a frozen dataclass with the research note's defaults
``calendar``    the run's own trading calendar: muhurat and special sessions removed
``indicators``  per-bar columns the rules read, computed against that calendar
``signals``     Chartink's five lines and the six trend filters that make them a strategy
``breadth``     the share of the universe above its 200-day average, and the gate it implies
``sizing``      ten equal slots → a share count, with the cap that bound named
``exits``       the 12% stop, the 21-EMA exit, and what one close says about one position
``orders``      the limit that works for three **sessions** and is then cancelled
``plan``        signals + book + gate → a plan the desk can confirm line by line
``backtest``    `04` §11: the study, re-run by every one of the functions above
"""

from __future__ import annotations

from baskfy_core.vbt.backtest import (
    BacktestParams,
    BacktestResult,
    BacktestStats,
    BacktestTrade,
    Panel,
    YearRow,
    gate_vector,
    panel_from_frame,
    run_backtest,
    summarise,
    yearly,
)
from baskfy_core.vbt.breadth import (
    BreadthReading,
    breadth_above_dma,
    breadth_series,
    gate_for,
)
from baskfy_core.vbt.calendar import (
    SessionCalendar,
    build_calendar,
    drop_thin_sessions,
    session_counts,
    thin_sessions,
)
from baskfy_core.vbt.config import (
    DEFAULT_VBT_CONFIG,
    TICK_INR,
    BreadthConfig,
    CostConfig,
    DataConfig,
    EntryConfig,
    ExitConfig,
    Gate,
    ScanConfig,
    SignalState,
    SizingConfig,
    TrendConfig,
    TrendFilter,
    VbtConfig,
)
from baskfy_core.vbt.exits import (
    Action,
    Bar,
    ExitReason,
    ManageAction,
    OpenPosition,
    apply_stop,
    initial_stop,
    manage,
    r_multiple,
    stop_fill,
    tick_floor,
)
from baskfy_core.vbt.indicators import (
    INDICATOR_COLUMNS,
    OPTIONAL_COLUMNS,
    REQUIRED_COLUMNS,
    densify,
    require_columns,
    with_vbt_indicators,
)
from baskfy_core.vbt.orders import (
    LIVE_STATES,
    TERMINAL_STATES,
    CancelReason,
    Fill,
    OrderState,
    WorkingOrder,
    advance_session,
    apply_fill,
    expire_orders,
    expires_after,
    fill_if_touched,
    is_expired,
    sessions_since,
)
from baskfy_core.vbt.plan import (
    LINE_ORDER,
    BookState,
    Candidate,
    LineKind,
    PlanLine,
    Skipped,
    SkipReason,
    VbtPlan,
    assemble,
    build_entries,
    exit_lines,
    to_tick,
)
from baskfy_core.vbt.signals import (
    chartink_scan,
    detect_signals,
    signal_mask,
    trend_filters,
    with_signal_columns,
)
from baskfy_core.vbt.sizing import (
    SizeCap,
    SizedEntry,
    SizeRefusal,
    first_live_multiplier,
    size_entry,
)

__all__ = [
    "DEFAULT_VBT_CONFIG",
    "INDICATOR_COLUMNS",
    "LINE_ORDER",
    "LIVE_STATES",
    "OPTIONAL_COLUMNS",
    "REQUIRED_COLUMNS",
    "TERMINAL_STATES",
    "TICK_INR",
    "Action",
    "BacktestParams",
    "BacktestResult",
    "BacktestStats",
    "BacktestTrade",
    "Bar",
    "BookState",
    "BreadthConfig",
    "BreadthReading",
    "CancelReason",
    "Candidate",
    "CostConfig",
    "DataConfig",
    "EntryConfig",
    "ExitConfig",
    "ExitReason",
    "Fill",
    "Gate",
    "LineKind",
    "ManageAction",
    "OpenPosition",
    "OrderState",
    "Panel",
    "PlanLine",
    "ScanConfig",
    "SessionCalendar",
    "SignalState",
    "SizeCap",
    "SizeRefusal",
    "SizedEntry",
    "SizingConfig",
    "SkipReason",
    "Skipped",
    "TrendConfig",
    "TrendFilter",
    "VbtConfig",
    "VbtPlan",
    "WorkingOrder",
    "YearRow",
    "advance_session",
    "apply_fill",
    "apply_stop",
    "assemble",
    "breadth_above_dma",
    "breadth_series",
    "build_calendar",
    "build_entries",
    "chartink_scan",
    "densify",
    "detect_signals",
    "drop_thin_sessions",
    "exit_lines",
    "expire_orders",
    "expires_after",
    "fill_if_touched",
    "first_live_multiplier",
    "gate_for",
    "gate_vector",
    "initial_stop",
    "is_expired",
    "manage",
    "panel_from_frame",
    "r_multiple",
    "require_columns",
    "run_backtest",
    "session_counts",
    "sessions_since",
    "signal_mask",
    "size_entry",
    "stop_fill",
    "summarise",
    "thin_sessions",
    "tick_floor",
    "to_tick",
    "trend_filters",
    "with_signal_columns",
    "with_vbt_indicators",
    "yearly",
]
