"""``baskfy_core.twt`` — TWT-1, the "three weeks tight" position sleeve, as pure functions.

Specification: ``docs/twt/`` at the repo root (``01-method.md`` is the method, taken from
``research/tight-close/STRATEGY.md``; ``04-business-rules.md`` is the numerical contract every test
here asserts). The package obeys law 1 of ``CLAUDE.md``: DataFrames and dataclasses in, DataFrames
and dataclasses out — **no database, no network, no disk, no clock**. The worker, the API and the
desk own every read, write and timestamp.

One sentence of the strategy: a name that has run 30 % above its low of three months ago and whose
last three weekly closes sit within 3.01 % of each other, bought at the next session's open the
first day the state is true after five sessions out, in a name turning over ₹5 crore a day, with no
new entry unless more than 40 % of the traded universe is above its own 200-session average —
stopped 20 % below the fill and trailed 20 % below the highest high since, and **exited by nothing
else**.

Module map
----------
``config``      every threshold, as a frozen dataclass with ``04``'s values
``calendar``    the run's own trading calendar: muhurat and special sessions removed — VBT-1's
                implementation, TWT's own thresholds
``indicators``  per-bar columns the rules read, computed against that calendar, plus the ISO-week
                and calendar-month buckets §3.2 and §3.3 are about
``signals``     the three weekly closes, the month-3 low, the state, and the event inside it
``breadth``     the share of the universe above its 200-session average, and the gate it implies —
                VBT-1's arithmetic, TWT's own spelled-out thresholds (``04`` §4.4)
``sizing``      ten equal slots -> a share count, with every cap applied in ``04`` §6.2's order
``exits``       the 20 % stop, the ratcheting 20 % trail, the fill-day rule and the write-off
``sleeve``      the sleeve's own cash and equity — never the account's
``plan``        signals + book + gate -> a plan the desk can confirm line by line
"""

from __future__ import annotations

from baskfy_core.twt.breadth import (
    BreadthReading,
    breadth_above_dma,
    breadth_series,
    gate_for,
)
from baskfy_core.twt.calendar import (
    SessionCalendar,
    build_calendar,
    drop_thin_sessions,
    session_counts,
    thin_sessions,
)
from baskfy_core.twt.config import (
    DEFAULT_TWT_CONFIG,
    DRY_RUN_SESSIONS_REQUIRED,
    RESEARCH_TICK_INR,
    TICK_INR,
    BacktestConfig,
    BreadthConfig,
    CostConfig,
    DataConfig,
    EntryConfig,
    EntryTiming,
    ExitConfig,
    Gate,
    RankKey,
    ScanConfig,
    SignalState,
    SizingConfig,
    TwtConfig,
)
from baskfy_core.twt.exits import (
    Action,
    AdjustmentOutcome,
    Bar,
    ExitReason,
    ManageAction,
    OpenPosition,
    Ratchet,
    clamp_under_close,
    fill_day_stop,
    initial_stop,
    manage,
    on_adjustment,
    r_multiple,
    ratchet,
    stop_fill,
    stop_is_below_entry,
    tick,
    tick_floor,
    trail_from,
)
from baskfy_core.twt.indicators import (
    INDICATOR_COLUMNS,
    OPTIONAL_COLUMNS,
    REQUIRED_COLUMNS,
    bucket_keys,
    require_columns,
    with_twt_indicators,
)
from baskfy_core.twt.plan import (
    EXIT_LINE_KINDS,
    LINE_ORDER,
    BookState,
    Candidate,
    EntryBar,
    LineKind,
    NakedPosition,
    PlanLine,
    RatchetDue,
    Skipped,
    SkipReason,
    TwtPlan,
    assemble,
    build_entries,
    client_id_for,
    exit_lines,
    gtt_limit_price,
    stop_for,
)
from baskfy_core.twt.signals import (
    SIGNAL_COLUMNS,
    WEEK_POSITION,
    WEEKLY_CLOSE_PREFIX,
    detect_signals,
    entry_events,
    liquidity_floor,
    month_low_back,
    signal_mask,
    tight_state,
    weekly_closes,
    weekly_column,
    with_twt_columns,
)
from baskfy_core.twt.sizing import (
    SizeCap,
    SizedEntry,
    SizeRefusal,
    first_live_multiplier,
    size_entry,
)
from baskfy_core.twt.sleeve import (
    MarkSource,
    OpenPositionValue,
    SleeveValue,
    sleeve_value,
)

__all__ = [
    "DEFAULT_TWT_CONFIG",
    "DRY_RUN_SESSIONS_REQUIRED",
    "EXIT_LINE_KINDS",
    "INDICATOR_COLUMNS",
    "LINE_ORDER",
    "OPTIONAL_COLUMNS",
    "REQUIRED_COLUMNS",
    "RESEARCH_TICK_INR",
    "SIGNAL_COLUMNS",
    "TICK_INR",
    "WEEKLY_CLOSE_PREFIX",
    "WEEK_POSITION",
    "Action",
    "AdjustmentOutcome",
    "BacktestConfig",
    "Bar",
    "BookState",
    "BreadthConfig",
    "BreadthReading",
    "Candidate",
    "CostConfig",
    "DataConfig",
    "EntryBar",
    "EntryConfig",
    "EntryTiming",
    "ExitConfig",
    "ExitReason",
    "Gate",
    "LineKind",
    "ManageAction",
    "MarkSource",
    "NakedPosition",
    "OpenPosition",
    "OpenPositionValue",
    "PlanLine",
    "RankKey",
    "Ratchet",
    "RatchetDue",
    "ScanConfig",
    "SessionCalendar",
    "SignalState",
    "SizeCap",
    "SizeRefusal",
    "SizedEntry",
    "SizingConfig",
    "SkipReason",
    "Skipped",
    "SleeveValue",
    "TwtConfig",
    "TwtPlan",
    "assemble",
    "breadth_above_dma",
    "breadth_series",
    "bucket_keys",
    "build_calendar",
    "build_entries",
    "clamp_under_close",
    "client_id_for",
    "detect_signals",
    "drop_thin_sessions",
    "entry_events",
    "exit_lines",
    "fill_day_stop",
    "first_live_multiplier",
    "gate_for",
    "gtt_limit_price",
    "initial_stop",
    "liquidity_floor",
    "manage",
    "month_low_back",
    "on_adjustment",
    "r_multiple",
    "ratchet",
    "require_columns",
    "session_counts",
    "signal_mask",
    "size_entry",
    "sleeve_value",
    "stop_fill",
    "stop_for",
    "stop_is_below_entry",
    "thin_sessions",
    "tick",
    "tick_floor",
    "tight_state",
    "trail_from",
    "weekly_closes",
    "weekly_column",
    "with_twt_columns",
    "with_twt_indicators",
]
