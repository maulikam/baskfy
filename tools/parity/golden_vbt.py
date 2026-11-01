"""The VBT lane of the parity goldens: ``baskfy_core.vbt`` (VBT-1, the volume breakout), wave 3 of
the Rust rewrite's answer key (29 Oct 2026).

    cd decile-blueprint
    PYTHONPATH=..:packages/core/src:packages/providers/src:packages/execution/src \\
        .venv/bin/python ../tools/parity/golden_vbt.py

Dumps the public pure functions of ``baskfy_core.vbt`` into
``go/testdata/golden/L1/vbt/<module>.<function>.case_NNN.json``, written ``stable=True`` by
``tools/parity/golden.py``'s own encoder, so two runs are byte-identical:

* ``config`` — ``DataConfig.min_samples`` (Python's half-even ``round``), ``VbtConfig.bars_required``,
  ``filter_legend``;
* ``calendar`` — ``session_counts``, ``thin_sessions``, ``build_calendar``, ``drop_thin_sessions``
  and ``SessionCalendar.index_of`` / ``sessions_between`` / ``advance``;
* ``indicators`` — ``require_columns``, ``densify``, ``with_vbt_indicators``;
* ``signals`` — ``chartink_scan`` and ``trend_filters`` (each an expression, so the golden is the
  expression **evaluated over the stored frame**), ``with_signal_columns``, ``detect_signals``,
  ``signal_mask``;
* ``breadth`` — ``gate_for``, ``breadth_above_dma``, ``breadth_series``;
* ``sizing`` — ``size_entry`` (every cap, every refusal), ``first_live_multiplier``;
* ``exits`` — ``tick_floor``, ``initial_stop``, ``apply_stop``, ``stop_fill``, ``manage`` (every
  action and reason it can return), ``r_multiple``;
* ``orders`` — ``expires_after``, ``sessions_since``, ``is_expired``, ``advance_session``,
  ``expire_orders``, ``fill_if_touched``, ``apply_fill``;
* ``sleeve`` — ``sleeve_value`` (with every derived property and ``quantize()``);
* ``plan`` — ``to_tick``, ``BookState.entries_in``/``slots_taken``, ``build_entries`` (every
  ``SkipReason``), ``exit_lines``, ``assemble`` (with ``plan_hash`` and the entry totals);
* ``drift`` — ``compare``; ``published`` — the ``PUBLISHED`` record;
* ``backtest`` — ``panel_from_frame``, ``gate_vector``, ``run_backtest`` (every exit reason and
  every skip counter), ``summarise``, ``yearly``, ``BacktestResult.fill_rate_pct``.

Conventions the reimplementation must not misread (restated in every case's ``meta.notes``):

* Every case's inputs are JSON, and every function is called from the stored inputs after a round
  trip through their JSON text — never from an object that was not written down.
* Decimals are strings; dates are ISO days; enums are their values; a polars frame is
  ``{"columns", "dtypes", "rows"}`` (golden.py's native encoding); a numpy matrix is a list of rows
  with ``NaN`` written as ``null``. A float is written with ``repr`` precision.
* ``config`` is a whole ``VbtConfig`` (``data``, ``scan``, ``trend``, ``breadth``, ``entry``,
  ``sizing``, ``exits`` with its nested swing ``StopConfig`` as ``exits.qulla``, ``costs``),
  decoded field by field by each sub-dataclass's **declared** type (golden.py's ``_sub_config``),
  so ``sizing.min_trade_value_inr`` comes back a ``Decimal``.
* A ``SessionCalendar`` is ``{"sessions": [...], "dropped": [...], "counts": {date: int}}``.
* A function that raises stores ``{"raises": {"type", "message"}}`` as its output.
* An expression-valued function (``chartink_scan``, ``trend_filters``) is golden as its value over
  ``inputs.frame``: a polars Series ``{"dtype", "values"}`` (``trend_filters``: one per letter).
* Synthetic bars come from ``numpy.random.default_rng(seed)`` rounded to 2 dp before they are
  written, so the stored inputs, not the generator, are the contract. ``run_backtest`` panels use
  prices on a quarter-rupee grid so ``Decimal(float)`` is exact and short.

Not dumped, and why: private helpers (``_prepare``, ``_true``, ``_latest_session``,
``_refusal_detail``, ``_entry_line``, ``_stop_for``, ``_budget``, ``_mark_equity``, ``_rank_of``,
``_fill_threshold``, ``_floor_to_tick``, ``_round_to_tick``) are exercised only through the public
functions that call them. ``Bar.history`` (LV9 Qullamaggie bars) is not read by ``vbt.exits.manage``
and is always empty here. Nothing in ``baskfy_core.vbt`` is impure, so nothing is skipped for I/O.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import importlib.util
import json
import math
import sys
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

HERE = Path(__file__).resolve().parent


def _golden() -> Any:
    name = "baskfy_parity_golden"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, HERE / "golden.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


LANE = "L1/vbt"
PREFIX = "baskfy_core.vbt."
NOTES = (
    "Inputs are JSON and each function is called from them after a JSON round trip. Decimals are "
    "strings; dates are ISO days; enums are their values; polars frames are {columns, dtypes, "
    "rows}; numpy matrices are row lists with NaN as null. config is a whole VbtConfig decoded "
    "by each sub-dataclass's declared field types (exits.qulla is the swing StopConfig). A "
    "SessionCalendar is {sessions, dropped, counts}. A raise is {raises: {type, message}}. "
    "Expression-valued functions are golden as their value over inputs.frame. Synthetic seeded "
    "fixtures, rounded before they are written. Regenerate with `PYTHONPATH=..:packages/core/src:"
    "packages/providers/src:packages/execution/src .venv/bin/python ../tools/parity/golden_vbt.py` "
    "from decile-blueprint."
)

D = Decimal


# =============================================================================================
# Decoders — stored JSON -> the objects each function takes
# =============================================================================================


def _date(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


def _odate(value: str | None) -> dt.date | None:
    return None if value is None else _date(value)


def _dec(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def frame_from(doc: dict[str, Any]) -> pl.DataFrame:
    return _golden().polars_frame_from(doc)


def vbt_config_from(doc: dict[str, Any] | None) -> Any:
    from baskfy_core.swing.config import StopConfig
    from baskfy_core.vbt import config as cfg

    if doc is None:
        return cfg.DEFAULT_VBT_CONFIG
    sub = _golden()._sub_config
    parts = {
        "data": cfg.DataConfig,
        "scan": cfg.ScanConfig,
        "trend": cfg.TrendConfig,
        "breadth": cfg.BreadthConfig,
        "entry": cfg.EntryConfig,
        "sizing": cfg.SizingConfig,
        "costs": cfg.CostConfig,
    }
    fields: dict[str, Any] = {k: sub(cls, doc[k]) for k, cls in parts.items()}
    exits = dict(doc["exits"])
    qulla = sub(StopConfig, exits.pop("qulla"))
    fields["exits"] = dataclasses.replace(sub(cfg.ExitConfig, exits), qulla=qulla)
    return cfg.VbtConfig(**fields)


def calendar_from(doc: dict[str, Any]) -> Any:
    from baskfy_core.vbt.calendar import SessionCalendar

    return SessionCalendar(
        sessions=tuple(_date(d) for d in doc["sessions"]),
        dropped=tuple(_date(d) for d in doc["dropped"]),
        counts={_date(k): int(v) for k, v in doc["counts"].items()},
    )


def working_order_from(doc: dict[str, Any]) -> Any:
    from baskfy_core.vbt.orders import CancelReason, OrderState, WorkingOrder

    return WorkingOrder(
        instrument_id=int(doc["instrument_id"]),
        signal_date=_date(doc["signal_date"]),
        limit_price=D(doc["limit_price"]),
        stop_price=D(doc["stop_price"]),
        quantity=int(doc["quantity"]),
        state=OrderState(doc["state"]),
        sessions_worked=int(doc["sessions_worked"]),
        filled_quantity=int(doc["filled_quantity"]),
        cancel_reason=None if doc["cancel_reason"] is None else CancelReason(doc["cancel_reason"]),
    )


def position_from(doc: dict[str, Any]) -> Any:
    from baskfy_core.vbt.exits import OpenPosition

    return OpenPosition(
        instrument_id=int(doc["instrument_id"]),
        entry_date=_date(doc["entry_date"]),
        entry_price=D(doc["entry_price"]),
        quantity=int(doc["quantity"]),
        stop_price=D(doc["stop_price"]),
        initial_stop=D(doc["initial_stop"]),
        exit_queued_for=_odate(doc["exit_queued_for"]),
    )


def bar_from(doc: dict[str, Any]) -> Any:
    from baskfy_core.vbt.exits import Bar

    assert doc["history"] == [], "vbt.exits.manage never reads Bar.history; the lane keeps it empty"
    return Bar(
        session=_date(doc["session"]),
        open=_dec(doc["open"]),
        high=_dec(doc["high"]),
        low=_dec(doc["low"]),
        close=_dec(doc["close"]),
        ema_exit=_dec(doc["ema_exit"]),
    )


def manage_action_from(doc: dict[str, Any]) -> Any:
    from baskfy_core.vbt.exits import Action, ExitReason, ManageAction

    return ManageAction(
        action=Action(doc["action"]),
        reason=None if doc["reason"] is None else ExitReason(doc["reason"]),
        price=_dec(doc["price"]),
        note=doc["note"],
        quantity=doc["quantity"],
        new_stop=_dec(doc["new_stop"]),
    )


def book_from(doc: dict[str, Any]) -> Any:
    from baskfy_core.vbt.plan import BookState

    return BookState(
        open_instrument_ids=frozenset(int(i) for i in doc["open_instrument_ids"]),
        working_instrument_ids=frozenset(int(i) for i in doc["working_instrument_ids"]),
        open_exposure_inr=D(doc["open_exposure_inr"]),
        cash_available_inr=D(doc["cash_available_inr"]),
        entries_already_this_session=int(doc["entries_already_this_session"]),
        positions_naked_of_gtt=tuple(
            (int(i), s, int(q), D(stop)) for i, s, q, stop in doc["positions_naked_of_gtt"]
        ),
        open_entry_counts={int(k): int(v) for k, v in doc["open_entry_counts"].items()},
    )


def candidate_from(doc: dict[str, Any]) -> Any:
    from baskfy_core.vbt.plan import Candidate

    return Candidate(
        instrument_id=int(doc["instrument_id"]),
        symbol=doc["symbol"],
        signal_date=_date(doc["signal_date"]),
        close_raw=D(doc["close_raw"]),
        turnover_avg_inr=_dec(doc["turnover_avg_inr"]),
        rank_key=int(doc["rank_key"]),
        locked_upper_circuit=bool(doc["locked_upper_circuit"]),
    )


def plan_line_from(doc: dict[str, Any]) -> Any:
    from baskfy_core.vbt.exits import ExitReason
    from baskfy_core.vbt.plan import LineKind, PlanLine
    from baskfy_core.vbt.sizing import SizeCap

    return PlanLine(
        kind=LineKind(doc["kind"]),
        instrument_id=int(doc["instrument_id"]),
        symbol=doc["symbol"],
        quantity=int(doc["quantity"]),
        limit_price=_dec(doc["limit_price"]),
        stop_price=_dec(doc["stop_price"]),
        value_inr=D(doc["value_inr"]),
        cap=None if doc["cap"] is None else SizeCap(doc["cap"]),
        reason=None if doc["reason"] is None else ExitReason(doc["reason"]),
        note=doc["note"],
        position_id=doc["position_id"],
    )


def skipped_from(doc: dict[str, Any]) -> Any:
    from baskfy_core.vbt.plan import Skipped, SkipReason

    return Skipped(int(doc["instrument_id"]), doc["symbol"], SkipReason(doc["reason"]), doc["detail"])


def _matrix(rows: list[list[float | None]], dtype: Any = float) -> np.ndarray:
    if dtype is bool:
        return np.array(rows, dtype=bool).reshape(len(rows), -1 if rows else 0)
    return np.array(
        [[math.nan if v is None else float(v) for v in row] for row in rows], dtype=np.float64
    ).reshape(len(rows), -1 if rows else 0)


PANEL_MATRICES = ("open", "high", "low", "close", "ema_exit", "turnover_avg", "turnover_inr")


def panel_from(doc: dict[str, Any]) -> Any:
    from baskfy_core.vbt.backtest import Panel

    return Panel(
        symbols=tuple(doc["symbols"]),
        instrument_ids=tuple(int(i) for i in doc["instrument_ids"]),
        sessions=tuple(_date(d) for d in doc["sessions"]),
        signal=_matrix(doc["signal"], bool),
        **{name: _matrix(doc[name]) for name in PANEL_MATRICES},
    )


def params_from(doc: dict[str, Any]) -> Any:
    from baskfy_core.vbt.backtest import BacktestParams

    return BacktestParams(
        sleeve_inr=D(doc["sleeve_inr"]),
        start=_odate(doc["start"]),
        end=_odate(doc["end"]),
        cost_pct_per_side=D(doc["cost_pct_per_side"]),
        tick=D(doc["tick"]),
        config=vbt_config_from(doc["config"]),
        label=doc["label"],
    )


def trade_from(doc: dict[str, Any]) -> Any:
    from baskfy_core.vbt.backtest import BacktestTrade
    from baskfy_core.vbt.exits import ExitReason
    from baskfy_core.vbt.sizing import SizeCap

    return BacktestTrade(
        symbol=doc["symbol"],
        instrument_id=int(doc["instrument_id"]),
        entry_date=_date(doc["entry_date"]),
        exit_date=_date(doc["exit_date"]),
        entry_price=D(doc["entry_price"]),
        exit_price=D(doc["exit_price"]),
        quantity=int(doc["quantity"]),
        pnl_inr=D(doc["pnl_inr"]),
        return_pct=D(doc["return_pct"]),
        r_multiple=_dec(doc["r_multiple"]),
        hold_sessions=int(doc["hold_sessions"]),
        reason=ExitReason(doc["reason"]),
        cap=None if doc["cap"] is None else SizeCap(doc["cap"]),
    )


def result_from(doc: dict[str, Any]) -> Any:
    from baskfy_core.vbt.backtest import BacktestResult

    return BacktestResult(
        params=params_from(doc["params"]),
        trades=tuple(trade_from(t) for t in doc["trades"]),
        sessions=tuple(_date(d) for d in doc["sessions"]),
        equity=tuple(D(v) for v in doc["equity"]),
        open_positions=tuple(int(v) for v in doc["open_positions"]),
        skipped={k: int(v) for k, v in doc["skipped"].items()},
        orders_offered=int(doc["orders_offered"]),
    )


def study_from(doc: dict[str, Any] | None) -> Any:
    from baskfy_core.vbt.published import PUBLISHED, PublishedStudy

    return PUBLISHED if doc is None else PublishedStudy(**doc)


# =============================================================================================
# Calls — stored inputs -> the output the golden stores
# =============================================================================================


def _raises(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except (ValueError, TypeError) as exc:
        return {"raises": {"type": type(exc).__name__, "message": str(exc)}}


def _calendar_out(cal: Any) -> dict[str, Any]:
    return {"sessions": cal.sessions, "dropped": cal.dropped, "counts": cal.counts}


def call_min_samples(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.config import DataConfig

    data = _golden()._sub_config(DataConfig, i["data"])
    return data.min_samples(int(i["window_bars"]))


def call_bars_required(i: dict[str, Any]) -> Any:
    return vbt_config_from(i["config"]).bars_required


def call_filter_legend(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.config import TrendConfig, filter_legend

    trend = None if i["trend"] is None else _golden()._sub_config(TrendConfig, i["trend"])
    return filter_legend(trend)


def call_session_counts(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.calendar import session_counts

    return _raises(lambda: session_counts(frame_from(i["bars"])))


def call_thin_sessions(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.calendar import thin_sessions
    from baskfy_core.vbt.config import DataConfig

    data = None if i["data"] is None else _golden()._sub_config(DataConfig, i["data"])
    return _raises(lambda: thin_sessions(frame_from(i["bars"]), data))


def call_build_calendar(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.calendar import build_calendar

    return _raises(
        lambda: _calendar_out(build_calendar(frame_from(i["bars"]), vbt_config_from(i["config"])))
    )


def call_drop_thin_sessions(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.calendar import drop_thin_sessions

    def run() -> Any:
        bars, cal = drop_thin_sessions(frame_from(i["bars"]), vbt_config_from(i["config"]))
        return {"bars": bars, "calendar": _calendar_out(cal)}

    return _raises(run)


def call_index_of(i: dict[str, Any]) -> Any:
    return calendar_from(i["calendar"]).index_of(_date(i["session"]))


def call_sessions_between(i: dict[str, Any]) -> Any:
    return calendar_from(i["calendar"]).sessions_between(_date(i["first"]), _date(i["last"]))


def call_advance(i: dict[str, Any]) -> Any:
    return calendar_from(i["calendar"]).advance(_date(i["session"]), int(i["sessions"]))


def call_require_columns(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.indicators import require_columns

    def run() -> Any:
        require_columns(frame_from(i["bars"]))
        return {"ok": True}

    return _raises(run)


def call_densify(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.indicators import densify

    return densify(frame_from(i["bars"]), calendar_from(i["calendar"]))


def call_with_vbt_indicators(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.indicators import with_vbt_indicators

    return _raises(
        lambda: with_vbt_indicators(
            frame_from(i["bars"]), calendar_from(i["calendar"]), vbt_config_from(i["config"])
        )
    )


def call_chartink_scan(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.signals import chartink_scan

    scan = vbt_config_from(i["config"]).scan
    return frame_from(i["frame"]).select(chartink_scan(scan).alias("scan_hit"))["scan_hit"]


def call_trend_filters(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.signals import trend_filters

    trend = vbt_config_from(i["config"]).trend
    frame = frame_from(i["frame"])
    return {
        letter.value: frame.select(expr.alias("f"))["f"]
        for letter, expr in trend_filters(trend).items()
    }


def call_with_signal_columns(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.signals import with_signal_columns

    return with_signal_columns(frame_from(i["frame"]), vbt_config_from(i["config"]))


def call_detect_signals(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.signals import detect_signals

    return detect_signals(
        frame_from(i["frame"]), _odate(i["as_of"]), vbt_config_from(i["config"])
    )


def call_signal_mask(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.signals import signal_mask

    return signal_mask(frame_from(i["frame"]), vbt_config_from(i["config"]))


def call_gate_for(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.breadth import gate_for
    from baskfy_core.vbt.config import BreadthConfig

    breadth = None if i["breadth"] is None else _golden()._sub_config(BreadthConfig, i["breadth"])
    return gate_for(D(i["pct_above_dma"]), breadth)


def call_breadth_above_dma(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.breadth import breadth_above_dma

    reading = breadth_above_dma(
        frame_from(i["frame"]), _odate(i["as_of"]), vbt_config_from(i["config"])
    )
    return {"reading": reading, "is_open": reading.is_open}


def call_breadth_series(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.breadth import breadth_series

    return breadth_series(frame_from(i["frame"]), vbt_config_from(i["config"]))


def call_size_entry(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.config import SizingConfig
    from baskfy_core.vbt.sizing import size_entry

    sized = size_entry(
        equity=D(i["equity"]),
        cash_available=D(i["cash_available"]),
        limit_price=D(i["limit_price"]),
        stop_price=D(i["stop_price"]),
        turnover_avg_inr=_dec(i["turnover_avg_inr"]),
        config=_golden()._sub_config(SizingConfig, i["config"]),
        slot_multiplier=D(i["slot_multiplier"]),
    )
    return {"sized": sized, "placed": sized.placed}


def call_first_live_multiplier(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.config import SizingConfig
    from baskfy_core.vbt.sizing import first_live_multiplier

    return first_live_multiplier(
        sessions_left=int(i["sessions_left"]),
        execution_enabled=bool(i["execution_enabled"]),
        config=_golden()._sub_config(SizingConfig, i["config"]),
    )


def call_tick_floor(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.exits import tick_floor

    return tick_floor(D(i["price"]), D(i["tick"]))


def call_initial_stop(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.exits import initial_stop

    return initial_stop(D(i["fill_price"]), vbt_config_from(i["config"]).exits, D(i["tick"]))


def call_apply_stop(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.exits import apply_stop

    return apply_stop(D(i["current"]), D(i["proposed"]))


def call_stop_fill(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.exits import stop_fill

    hit = stop_fill(bar_from(i["bar"]), D(i["stop"]))
    return None if hit is None else {"reason": hit[0], "price": hit[1]}


def call_manage(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.exits import manage

    return manage(
        position_from(i["position"]),
        bar_from(i["bar"]),
        blank_sessions=int(i["blank_sessions"]),
        config=vbt_config_from(i["config"]).exits,
    )


def call_r_multiple(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.exits import r_multiple

    return r_multiple(D(i["entry"]), D(i["initial"]), D(i["exit_price"]))


def call_expires_after(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.orders import expires_after

    entry = vbt_config_from(i["config"]).entry
    return expires_after(_date(i["signal_date"]), calendar_from(i["calendar"]), entry)


def call_sessions_since(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.orders import sessions_since

    return sessions_since(
        _date(i["signal_date"]), _date(i["session"]), calendar_from(i["calendar"])
    )


def call_is_expired(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.orders import is_expired

    return is_expired(
        working_order_from(i["order"]),
        _date(i["session"]),
        calendar_from(i["calendar"]),
        vbt_config_from(i["config"]).entry,
    )


def _order_out(order: Any) -> dict[str, Any]:
    return {"order": order, "is_live": order.is_live, "remaining": order.remaining}


def call_advance_session(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.orders import advance_session

    return _order_out(advance_session(working_order_from(i["order"])))


def call_expire_orders(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.orders import CancelReason, expire_orders

    live, done = expire_orders(
        [working_order_from(o) for o in i["orders"]],
        _date(i["session"]),
        calendar_from(i["calendar"]),
        vbt_config_from(i["config"]).entry,
        CancelReason(i["reason"]),
    )
    return {"live": live, "expired": done}


def call_fill_if_touched(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.orders import fill_if_touched

    return fill_if_touched(
        working_order_from(i["order"]),
        _date(i["session"]),
        open_price=_dec(i["open_price"]),
        high=_dec(i["high"]),
        low=_dec(i["low"]),
        config=vbt_config_from(i["config"]).entry,
    )


def call_apply_fill(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.orders import Fill, apply_fill

    order = working_order_from(i["order"])
    f = i["fill"]
    fill = Fill(order=order, quantity=int(f["quantity"]), price=D(f["price"]),
                session=_date(f["session"]))  # fmt: skip
    return _order_out(apply_fill(order, fill))


def call_sleeve_value(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.sleeve import OpenPositionValue, WorkingCommitment, sleeve_value

    value = sleeve_value(
        capital_inr=D(i["capital_inr"]),
        realised_inr=D(i["realised_inr"]),
        open_positions=[
            OpenPositionValue(int(p["instrument_id"]), int(p["quantity_open"]),
                              D(p["entry_avg"]), D(p["mark"]))
            for p in i["open_positions"]
        ],  # fmt: skip
        working_orders=[
            WorkingCommitment(int(o["instrument_id"]), int(o["quantity"]), D(o["limit_price"]))
            for o in i["working_orders"]
        ],
    )
    return {
        "value": value,
        "cash_inr": value.cash_inr,
        "cash_available_inr": value.cash_available_inr,
        "equity_inr": value.equity_inr,
        "open_exposure_inr": value.open_exposure_inr,
        "unrealised_inr": value.unrealised_inr,
        "quantized": value.quantize(),
    }


def call_to_tick(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.plan import to_tick

    return to_tick(D(i["price"]), _dec(i["tick"]))


def call_book_state(i: dict[str, Any]) -> Any:
    book = book_from(i["book"])
    return {
        "entries_in": {str(k): book.entries_in(int(k)) for k in i["instrument_ids"]},
        "slots_taken": book.slots_taken,
    }


def call_build_entries(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.config import Gate
    from baskfy_core.vbt.plan import build_entries

    lines, skips = build_entries(
        [candidate_from(c) for c in i["candidates"]],
        gate=Gate(i["gate"]),
        equity=D(i["equity"]),
        book=book_from(i["book"]),
        config=vbt_config_from(i["config"]),
        slot_multiplier=D(i["slot_multiplier"]),
        max_open_positions=i["max_open_positions"],
        tick=_dec(i["tick"]),
    )
    return {"lines": lines, "skipped": skips}


def call_exit_lines(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.plan import exit_lines

    managed = []
    for row in i["managed"]:
        base = (int(row[0]), row[1], int(row[2]), manage_action_from(row[3]))
        managed.append((*base, row[4]) if len(row) == 5 else base)  # noqa: PLR2004
    expired = [(working_order_from(o), s) for o, s in i["expired"]]
    return exit_lines(managed, expired, book_from(i["book"]))


def call_assemble(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.config import Gate
    from baskfy_core.vbt.plan import assemble

    plan = assemble(
        _date(i["session"]),
        Gate(i["gate"]),
        D(i["equity"]),
        [plan_line_from(x) for x in i["exits"]],
        [plan_line_from(x) for x in i["entries"]],
        [skipped_from(x) for x in i["skips"]],
        i["source"],
    )
    return {
        "plan": plan,
        "plan_hash": plan.plan_hash(),
        "canonical": [line.canonical() for line in plan.lines],
        "total_new_exposure_inr": plan.total_new_exposure_inr,
        "entries": plan.entries,
    }


def call_compare(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.drift import DRIFT_CAGR_POINTS, compare

    drift = compare(
        cagr_pct=float(i["cagr_pct"]),
        max_drawdown_pct=float(i["max_drawdown_pct"]),
        trades=int(i["trades"]),
        study=study_from(i["study"]),
        threshold=DRIFT_CAGR_POINTS if i["threshold"] is None else float(i["threshold"]),
    )
    return {"drift": drift, "json": drift.to_json()}


def call_published(_: dict[str, Any]) -> Any:
    from baskfy_core.vbt.published import PUBLISHED

    return {"study": PUBLISHED, "cagr": PUBLISHED.cagr}


def _panel_out(panel: Any) -> dict[str, Any]:
    return {
        "symbols": panel.symbols,
        "instrument_ids": panel.instrument_ids,
        "sessions": panel.sessions,
        "signal": panel.signal,
        **{name: getattr(panel, name) for name in PANEL_MATRICES},
    }


def call_panel_from_frame(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.backtest import panel_from_frame

    panel = panel_from_frame(frame_from(i["frame"]), i["signal_column"])
    return {
        "panel": _panel_out(panel),
        "column_of": {d: panel.column_of(_date(d)) for d in i["column_of"]},
    }


def call_gate_vector(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.backtest import gate_vector

    return gate_vector(frame_from(i["breadth"]), tuple(_date(d) for d in i["sessions"]))


def _result_out(result: Any) -> dict[str, Any]:
    return {
        "trades": result.trades,
        "sessions": result.sessions,
        "equity": result.equity,
        "open_positions": result.open_positions,
        "skipped": result.skipped,
        "orders_offered": result.orders_offered,
        "fill_rate_pct": result.fill_rate_pct(),
    }


def call_run_backtest(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.backtest import run_backtest

    gate = np.array(i["gate_open"], dtype=bool)
    return _result_out(run_backtest(panel_from(i["panel"]), gate, params_from(i["params"])))


def call_summarise(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.backtest import summarise

    stats = summarise(result_from(i["result"]))
    return None if stats is None else {"stats": stats, "json": stats.to_json()}


def call_yearly(i: dict[str, Any]) -> Any:
    from baskfy_core.vbt.backtest import yearly

    try:
        rows = yearly(result_from(i["result"]))
    except IndexError as exc:  # an empty curve: `result.equity[0]` (see the case's known_bug)
        return {"raises": {"type": type(exc).__name__, "message": str(exc)}}
    return {"rows": rows, "json": [r.to_json() for r in rows]}


def call_fill_rate(i: dict[str, Any]) -> Any:
    return result_from(i["result"]).fill_rate_pct()


CALLS: dict[str, Callable[[dict[str, Any]], Any]] = {
    PREFIX + "config.DataConfig.min_samples": call_min_samples,
    PREFIX + "config.VbtConfig.bars_required": call_bars_required,
    PREFIX + "config.filter_legend": call_filter_legend,
    PREFIX + "calendar.session_counts": call_session_counts,
    PREFIX + "calendar.thin_sessions": call_thin_sessions,
    PREFIX + "calendar.build_calendar": call_build_calendar,
    PREFIX + "calendar.drop_thin_sessions": call_drop_thin_sessions,
    PREFIX + "calendar.SessionCalendar.index_of": call_index_of,
    PREFIX + "calendar.SessionCalendar.sessions_between": call_sessions_between,
    PREFIX + "calendar.SessionCalendar.advance": call_advance,
    PREFIX + "indicators.require_columns": call_require_columns,
    PREFIX + "indicators.densify": call_densify,
    PREFIX + "indicators.with_vbt_indicators": call_with_vbt_indicators,
    PREFIX + "signals.chartink_scan": call_chartink_scan,
    PREFIX + "signals.trend_filters": call_trend_filters,
    PREFIX + "signals.with_signal_columns": call_with_signal_columns,
    PREFIX + "signals.detect_signals": call_detect_signals,
    PREFIX + "signals.signal_mask": call_signal_mask,
    PREFIX + "breadth.gate_for": call_gate_for,
    PREFIX + "breadth.breadth_above_dma": call_breadth_above_dma,
    PREFIX + "breadth.breadth_series": call_breadth_series,
    PREFIX + "sizing.size_entry": call_size_entry,
    PREFIX + "sizing.first_live_multiplier": call_first_live_multiplier,
    PREFIX + "exits.tick_floor": call_tick_floor,
    PREFIX + "exits.initial_stop": call_initial_stop,
    PREFIX + "exits.apply_stop": call_apply_stop,
    PREFIX + "exits.stop_fill": call_stop_fill,
    PREFIX + "exits.manage": call_manage,
    PREFIX + "exits.r_multiple": call_r_multiple,
    PREFIX + "orders.expires_after": call_expires_after,
    PREFIX + "orders.sessions_since": call_sessions_since,
    PREFIX + "orders.is_expired": call_is_expired,
    PREFIX + "orders.advance_session": call_advance_session,
    PREFIX + "orders.expire_orders": call_expire_orders,
    PREFIX + "orders.fill_if_touched": call_fill_if_touched,
    PREFIX + "orders.apply_fill": call_apply_fill,
    PREFIX + "sleeve.sleeve_value": call_sleeve_value,
    PREFIX + "plan.to_tick": call_to_tick,
    PREFIX + "plan.BookState": call_book_state,
    PREFIX + "plan.build_entries": call_build_entries,
    PREFIX + "plan.exit_lines": call_exit_lines,
    PREFIX + "plan.assemble": call_assemble,
    PREFIX + "drift.compare": call_compare,
    PREFIX + "published.PUBLISHED": call_published,
    PREFIX + "backtest.panel_from_frame": call_panel_from_frame,
    PREFIX + "backtest.gate_vector": call_gate_vector,
    PREFIX + "backtest.run_backtest": call_run_backtest,
    PREFIX + "backtest.summarise": call_summarise,
    PREFIX + "backtest.yearly": call_yearly,
    PREFIX + "backtest.BacktestResult.fill_rate_pct": call_fill_rate,
}


# =============================================================================================
# Fixtures — deterministic, synthetic
# =============================================================================================

START = dt.date(2024, 1, 1)


def weekdays(count: int, start: dt.date = START) -> list[dt.date]:
    out: list[dt.date] = []
    day = start
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += dt.timedelta(days=1)
    return out


BAR_SCHEMA: dict[str, Any] = {
    "instrument_id": pl.Int64,
    "symbol": pl.String,
    "date": pl.Date,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "close_raw": pl.Float64,
    "volume": pl.Float64,
    "upper_circuit": pl.Float64,
    "adj_factor": pl.Float64,
}


def bars_frame(rows: list[dict[str, Any]], columns: tuple[str, ...] | None = None) -> pl.DataFrame:
    cols = columns or tuple(BAR_SCHEMA)
    schema = {c: BAR_SCHEMA[c] for c in cols}
    return pl.DataFrame([[r.get(c) for c in cols] for r in rows], schema=schema, orient="row")


def walk(  # noqa: PLR0913 - a fixture is its knobs
    instrument_id: int,
    symbol: str,
    days: list[dt.date],
    *,
    seed: int,
    start: float = 100.0,
    drift: float = 0.002,
    vol: float = 0.012,
    volume: float = 300_000.0,
    spikes: dict[int, tuple[float, float]] | None = None,
    holes: set[int] | None = None,
    adj: dict[int, float] | None = None,
    circuit: dict[int, float] | None = None,
) -> list[dict[str, Any]]:
    """One seeded instrument. ``spikes`` maps a session index to (change, volume multiple): a
    strong bar closing near its high. Every price is rounded to 2 dp before it is stored."""
    rng = np.random.default_rng(seed)
    rows: list[dict[str, Any]] = []
    level = start
    spikes = spikes or {}
    for k, day in enumerate(days):
        gap = float(rng.normal(0.0, 0.003))
        move = float(rng.normal(drift, vol))
        wick = abs(float(rng.normal(0.0, 0.004)))
        vol_mult = 1.0 + float(rng.uniform(-0.2, 0.2))
        o = round(level * (1.0 + gap), 2)
        if k in spikes:
            change, mult = spikes[k]
            c = round(level * (1.0 + change), 2)
            o = round(level * 1.005, 2)
            h = round(c * 1.002, 2)
            lo = round(min(o, c) * 0.995, 2)
            v = round(volume * mult)
        else:
            c = round(o * (1.0 + move), 2)
            h = round(max(o, c) * (1.0 + wick), 2)
            lo = round(min(o, c) * (1.0 - wick), 2)
            v = round(volume * vol_mult)
        level = c
        if holes and k in holes:
            continue
        factor = (adj or {}).get(k, 1.0)
        rows.append({
            "instrument_id": instrument_id, "symbol": symbol, "date": day,
            "open": o, "high": h, "low": lo, "close": c,
            "close_raw": round(c / factor, 2), "volume": float(v),
            "upper_circuit": (circuit or {}).get(k), "adj_factor": factor,
        })  # fmt: skip
    return rows


def _cfg_json(cfg: Any) -> dict[str, Any]:
    golden = _golden()
    return json.loads(golden.render(golden._enc(cfg)))


def default_config() -> Any:
    from baskfy_core.vbt.config import DEFAULT_VBT_CONFIG

    return DEFAULT_VBT_CONFIG


def small_config(**over: Any) -> Any:
    """A compressed VBT-1 so a 30-session fixture exercises every window: the same rules, short
    windows. Every field still comes from the dataclasses, written into the inputs."""
    from baskfy_core.vbt import config as c

    base = c.VbtConfig(
        data=c.DataConfig(thin_session_window_bars=5, thin_session_min_periods=3),
        scan=c.ScanConfig(vol_sma_bars=10),
        trend=c.TrendConfig(dma_bars=20, breakout_high_bars=5, ret_bars=5, turnover_bars=5),
        breadth=c.BreadthConfig(dma_bars=20),
        exits=c.ExitConfig(trail_ema_bars=5),
    )
    for part, fields in over.items():
        base = dataclasses.replace(base, **{part: dataclasses.replace(getattr(base, part), **fields)})
    return base


def with_config(part: str, **fields: Any) -> Any:
    cfg = default_config()
    return dataclasses.replace(cfg, **{part: dataclasses.replace(getattr(cfg, part), **fields)})


def cal_json(cal: Any) -> dict[str, Any]:
    return {"sessions": list(cal.sessions), "dropped": list(cal.dropped), "counts": cal.counts}


def make_cal(days: list[dt.date], dropped: tuple[dt.date, ...] = ()) -> dict[str, Any]:
    return {"sessions": [d for d in days if d not in dropped], "dropped": list(dropped),
            "counts": {d: 10 for d in days}}  # fmt: skip


def order_json(  # noqa: PLR0913
    instrument_id: int = 1,
    signal_date: dt.date = dt.date(2024, 1, 2),
    limit: str = "100.00",
    stop: str = "88.00",
    quantity: int = 100,
    state: str = "PROPOSED",
    sessions_worked: int = 0,
    filled: int = 0,
    cancel_reason: str | None = None,
) -> dict[str, Any]:
    return {"instrument_id": instrument_id, "signal_date": signal_date, "limit_price": limit,
            "stop_price": stop, "quantity": quantity, "state": state,
            "sessions_worked": sessions_worked, "filled_quantity": filled,
            "cancel_reason": cancel_reason}  # fmt: skip


def book_json(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "open_instrument_ids": [], "working_instrument_ids": [], "open_exposure_inr": "0",
        "cash_available_inr": "1000000", "entries_already_this_session": 0,
        "positions_naked_of_gtt": [], "open_entry_counts": {},
    }  # fmt: skip
    base.update(kw)
    return base


def cand(  # noqa: PLR0913
    iid: int, symbol: str, close: str, rank: int, turnover: str | None = "500000000",
    locked: bool = False,
) -> dict[str, Any]:
    return {"instrument_id": iid, "symbol": symbol, "signal_date": dt.date(2026, 10, 27),
            "close_raw": close, "turnover_avg_inr": turnover, "rank_key": rank,
            "locked_upper_circuit": locked}  # fmt: skip


def act(  # noqa: PLR0913
    action: str, reason: str | None = None, price: str | None = None, note: str = "",
    quantity: int | None = None, new_stop: str | None = None,
) -> dict[str, Any]:
    return {"action": action, "reason": reason, "price": price, "note": note,
            "quantity": quantity, "new_stop": new_stop}  # fmt: skip


def line(  # noqa: PLR0913
    kind: str, iid: int, symbol: str, quantity: int, *, limit: str | None = None,
    stop: str | None = None, value: str = "0", cap: str | None = None, reason: str | None = None,
    note: str = "", position_id: int | None = None,
) -> dict[str, Any]:
    return {"kind": kind, "instrument_id": iid, "symbol": symbol, "quantity": quantity,
            "limit_price": limit, "stop_price": stop, "value_inr": value, "cap": cap,
            "reason": reason, "note": note, "position_id": position_id}  # fmt: skip


INDICATED_SCHEMA: dict[str, Any] = {
    "instrument_id": pl.Int64,
    "date": pl.Date,
    "close": pl.Float64,
    "close_raw": pl.Float64,
    "volume": pl.Float64,
    "vol_sma": pl.Float64,
    "change_pct": pl.Float64,
    "sma_dma": pl.Float64,
    "high_prior": pl.Float64,
    "ret_lookback_pct": pl.Float64,
    "close_position": pl.Float64,
    "turnover_avg": pl.Float64,
    "turnover_inr": pl.Float64,
}

#: A row that passes every line and every filter under the default config.
PASSING: dict[str, Any] = {
    "close": 110.0, "close_raw": 110.0, "volume": 400_000.0, "vol_sma": 100_000.0,
    "change_pct": 8.0, "sma_dma": 90.0, "high_prior": 105.0, "ret_lookback_pct": 12.0,
    "close_position": 0.8, "turnover_avg": 3e7, "turnover_inr": 4.4e7,
}  # fmt: skip


def indicated(rows: list[dict[str, Any]]) -> pl.DataFrame:
    """Hand-built indicated rows. Each row is ``PASSING`` with overrides; ``instrument_id`` and
    ``date`` default to the row's position."""
    out = []
    for k, over in enumerate(rows):
        row = {"instrument_id": k + 1, "date": dt.date(2024, 6, 3), **PASSING, **over}
        out.append([row[c] for c in INDICATED_SCHEMA])
    return pl.DataFrame(out, schema=INDICATED_SCHEMA, orient="row")


# --- backtest panels ---------------------------------------------------------------------------


def panel_json(  # noqa: PLR0913
    days: list[dt.date],
    names: list[dict[str, Any]],
) -> dict[str, Any]:
    """A panel from per-name dicts: ``id``, ``symbol``, and per-session lists ``open``/``high``/
    ``low``/``close``/``ema``/``tavg``/``tinr``/``signal`` (``None`` = no bar). Quarter-rupee grid."""
    n = len(days)
    out: dict[str, Any] = {
        "symbols": [x["symbol"] for x in names],
        "instrument_ids": [x["id"] for x in names],
        "sessions": days,
    }
    keymap = {"open": "open", "high": "high", "low": "low", "close": "close", "ema_exit": "ema",
              "turnover_avg": "tavg", "turnover_inr": "tinr"}  # fmt: skip
    for name, key in keymap.items():
        out[name] = [list(x[key]) for x in names]
        assert all(len(r) == n for r in out[name])
    out["signal"] = [list(x["signal"]) for x in names]
    return out


def flat_name(  # noqa: PLR0913
    iid: int, symbol: str, n: int, *, price: float = 100.0, ema: float = 98.0,
    tavg: float = 5e8, tinr: float = 6e8,
) -> dict[str, Any]:
    """A name at ``price`` every session with its EMA below: open = close = price, high = price + 1,
    low = price - 0.5 — so a limit at a signal close fills on the next session unless a case
    raises that session's low."""
    return {
        "id": iid, "symbol": symbol,
        "open": [price] * n, "high": [price + 1.0] * n, "low": [price - 0.5] * n,
        "close": [price] * n, "ema": [ema] * n, "tavg": [tavg] * n, "tinr": [tinr] * n,
        "signal": [False] * n,
    }  # fmt: skip


def set_bar(name: dict[str, Any], col: int, **vals: Any) -> dict[str, Any]:
    for key, value in vals.items():
        name[key][col] = value
    return name


def blank(name: dict[str, Any], cols: range | list[int]) -> dict[str, Any]:
    for col in cols:
        for key in ("open", "high", "low", "close", "ema"):
            name[key][col] = None
    return name


def params_json(config: Any = None, **kw: Any) -> dict[str, Any]:
    base = {"sleeve_inr": "1000000", "start": None, "end": None, "cost_pct_per_side": "0.25",
            "tick": "0.01", "config": _cfg_json(config or default_config()), "label": "full"}  # fmt: skip
    base.update(kw)
    return base


# =============================================================================================
# Cases
# =============================================================================================

Case = tuple[str, str, dict[str, Any], str | None]

#: The one place the live Python is wrong-shaped against its own docstring, recorded rather
#: than fixed (this lane never edits Python): ``summarise`` returns ``None`` for an empty run,
#: but ``yearly`` indexes ``result.equity[0]`` before looking and raises ``IndexError``.
YEARLY_EMPTY_BUG = (
    "yearly() raises IndexError on a result with no sessions (it reads result.equity[0] "
    "unguarded), where summarise() returns None for the same input. The golden records the "
    "raise; a port should reproduce it until the Python is fixed."
)


def cases() -> list[Case]:  # noqa: PLR0915 - one block per function, in module order
    out: list[Case] = []

    def add(fn: str, why: str, inputs: dict[str, Any], known_bug: str | None = None) -> None:
        assert PREFIX + fn in CALLS, fn
        out.append((PREFIX + fn, why, inputs, known_bug))

    default_json = _cfg_json(default_config())
    small = small_config()
    small_json = _cfg_json(small)

    # ---------------------------------------------------------------- config
    from baskfy_core.vbt.config import DataConfig, TrendConfig

    data_default = _cfg_json(DataConfig())
    for window, share, why in (
        (1, 0.9, "a one-bar window still needs two samples (the max(2, ...) floor)"),
        (2, 0.9, "round(1.8) = 2"),
        (3, 0.9, "round(2.7) = 3: a full short window"),
        (5, 0.5, "round(2.5) is 2 under Python's half-even round, never 3"),
        (7, 0.5, "round(3.5) is 4 under half-even"),
        (20, 0.9, "the filter windows: round(18.0) = 18"),
        (41, 0.9, "round(36.9) = 37"),
        (50, 0.9, "the volume SMA: 45 of 50"),
        (200, 0.9, "the 200-DMA: 180 of 200"),
        (200, 1.0, "a demand for full windows"),
        (0, 0.9, "a zero window still demands the floor of two"),
    ):
        add("config.DataConfig.min_samples", why,
            {"data": {**data_default, "rolling_min_share": share}, "window_bars": window})  # fmt: skip

    add("config.VbtConfig.bars_required", "the default config: the 200-DMA dominates, +1", {"config": default_json})
    add("config.VbtConfig.bars_required", "the compressed config the lane's fixtures use", {"config": small_json})
    add("config.VbtConfig.bars_required", "breakout_high_bars + 1 dominates every window",
        {"config": _cfg_json(small_config(trend={"breakout_high_bars": 300}))})  # fmt: skip
    add("config.VbtConfig.bars_required", "the exit EMA dominates",
        {"config": _cfg_json(small_config(exits={"trail_ema_bars": 64}))})  # fmt: skip
    add("config.VbtConfig.bars_required", "the breadth DMA dominates the trend DMA",
        {"config": _cfg_json(with_config("breadth", dma_bars=250))})  # fmt: skip

    add("config.filter_legend", "None: the default TrendConfig's legend", {"trend": None})
    add("config.filter_legend", "the default config passed explicitly", {"trend": _cfg_json(TrendConfig())})
    add("config.filter_legend", "a recalibrated legend: Rs 1.5 crore, 22.5%, 0.55, 12%",
        {"trend": _cfg_json(TrendConfig(dma_bars=150, breakout_high_bars=55, ret_bars=10,
                                        max_ret_20_pct=22.5, min_close_position=0.55,
                                        max_change_pct=12.0, turnover_bars=10,
                                        min_turnover_avg_inr=1.5e7))})  # fmt: skip
    add("config.filter_legend", "a turnover floor of Rs 0.25 crore (the :g format)",
        {"trend": _cfg_json(TrendConfig(min_turnover_avg_inr=2.5e6, max_ret_20_pct=30.0))})  # fmt: skip

    # ---------------------------------------------------------------- calendar
    def date_frame(counts: dict[dt.date, int]) -> pl.DataFrame:
        rows = [[k + 1, d] for d, c in counts.items() for k in range(c)]
        return pl.DataFrame(rows, schema={"instrument_id": pl.Int64, "date": pl.Date}, orient="row")

    days30 = weekdays(30)
    rng = np.random.default_rng(20261029)
    base_counts = {d: int(rng.integers(18, 23)) for d in days30}
    muhurat = dict(base_counts)
    muhurat[days30[14]] = 3  # a thin session in the middle
    muhurat[days30[0]] = 2  # thin at the very start: the centred median still has min_periods
    two_thin = dict(base_counts)
    two_thin[days30[9]] = 3
    two_thin[days30[10]] = 4
    boundary = {d: 20 for d in days30}
    boundary[days30[15]] = 5  # exactly 0.25 x 20: not below, so kept
    boundary[days30[20]] = 4  # 4 < 5: thin
    few = {d: 20 for d in days30[:4]}
    few[days30[2]] = 1
    empty_dates = pl.DataFrame(schema={"instrument_id": pl.Int64, "date": pl.Date})
    shuffled = date_frame(base_counts).sample(fraction=1.0, shuffle=True, seed=7)
    small_data = _cfg_json(small.data)

    add("calendar.session_counts", "a 30-session universe, counted and sorted", {"bars": date_frame(base_counts)})
    add("calendar.session_counts", "rows arrive shuffled; the output is still sorted by date", {"bars": shuffled})
    add("calendar.session_counts", "an empty frame with a date column", {"bars": empty_dates})
    add("calendar.session_counts", "a single session", {"bars": date_frame({days30[0]: 7})})
    add("calendar.session_counts", "no date column raises ValueError",
        {"bars": pl.DataFrame({"instrument_id": [1, 2]}, schema={"instrument_id": pl.Int64})})  # fmt: skip

    add("calendar.thin_sessions", "no thin session in an ordinary month",
        {"bars": date_frame(base_counts), "data": None})  # fmt: skip
    add("calendar.thin_sessions", "a muhurat-shaped session mid-month and a thin first session",
        {"bars": date_frame(muhurat), "data": None})  # fmt: skip
    add("calendar.thin_sessions", "two adjacent thin sessions, both found",
        {"bars": date_frame(two_thin), "data": None})  # fmt: skip
    add("calendar.thin_sessions", "exactly 25% of the median is kept (strict <); 20% is dropped",
        {"bars": date_frame(boundary), "data": None})  # fmt: skip
    add("calendar.thin_sessions", "four sessions, below min_periods=5: no median, nothing dropped",
        {"bars": date_frame(few), "data": None})  # fmt: skip
    add("calendar.thin_sessions", "the same four sessions with min_periods=3: the thin one is found",
        {"bars": date_frame(few), "data": small_data})  # fmt: skip
    add("calendar.thin_sessions", "an empty frame is no thin sessions", {"bars": empty_dates, "data": None})
    add("calendar.thin_sessions", "a 5-session centred window and a 0.5 share",
        {"bars": date_frame(two_thin),
         "data": {**small_data, "thin_session_min_share": 0.5}})  # fmt: skip

    add("calendar.build_calendar", "an ordinary month: every session kept",
        {"bars": date_frame(base_counts), "config": default_json})  # fmt: skip
    add("calendar.build_calendar", "the muhurat session and the thin first session dropped",
        {"bars": date_frame(muhurat), "config": default_json})  # fmt: skip
    add("calendar.build_calendar", "two adjacent thin sessions dropped", {"bars": date_frame(two_thin), "config": default_json})
    add("calendar.build_calendar", "an empty frame: an empty calendar", {"bars": empty_dates, "config": default_json})
    add("calendar.build_calendar", "too few sessions for a median under the compressed config",
        {"bars": date_frame(few), "config": small_json})  # fmt: skip

    add("calendar.drop_thin_sessions", "nothing thin: the bars come back unchanged",
        {"bars": date_frame({d: 3 for d in days30[:8]}), "config": default_json})  # fmt: skip
    add("calendar.drop_thin_sessions", "the thin sessions' rows are removed",
        {"bars": date_frame(muhurat), "config": default_json})  # fmt: skip
    add("calendar.drop_thin_sessions", "an empty frame", {"bars": empty_dates, "config": default_json})
    add("calendar.drop_thin_sessions", "the boundary month: only the 20% session goes",
        {"bars": date_frame(boundary), "config": default_json})  # fmt: skip

    # Session calendar methods: a week with a holiday (2024-01-03 missing) and a dropped session.
    week = [dt.date(2024, 1, d) for d in (1, 2, 4, 5, 8, 9, 10)]
    cal_w = make_cal(week, dropped=(dt.date(2024, 1, 5),))
    for session, why in (
        (dt.date(2024, 1, 1), "the first session is index 0"),
        (dt.date(2024, 1, 4), "after a holiday the index does not skip"),
        (dt.date(2024, 1, 3), "a holiday is not a session: -1"),
        (dt.date(2024, 1, 5), "a dropped thin session is not a session: -1"),
        (dt.date(2024, 1, 10), "the last session"),
        (dt.date(2023, 12, 29), "before the calendar: -1"),
    ):
        add("calendar.SessionCalendar.index_of", why, {"calendar": cal_w, "session": session})
    for first, last, why in (
        (dt.date(2024, 1, 2), dt.date(2024, 1, 8), "(Tue, next Mon]: the holiday and the dropped "
                                                    "session count for nothing"),
        (dt.date(2024, 1, 4), dt.date(2024, 1, 4), "the same session: 0"),
        (dt.date(2024, 1, 9), dt.date(2024, 1, 2), "backwards: negative"),
        (dt.date(2024, 1, 3), dt.date(2024, 1, 8), "first is not a session: 0"),
        (dt.date(2024, 1, 2), dt.date(2024, 1, 12), "last is past the calendar: 0"),
    ):
        add("calendar.SessionCalendar.sessions_between", why,
            {"calendar": cal_w, "first": first, "last": last})  # fmt: skip
    for session, n, why in (
        (dt.date(2024, 1, 2), 3, "three sessions on, across the holiday and the dropped day"),
        (dt.date(2024, 1, 2), 0, "zero sessions is the session itself"),
        (dt.date(2024, 1, 8), 2, "exactly the last session"),
        (dt.date(2024, 1, 8), 3, "one past the end: None"),
        (dt.date(2024, 1, 3), 1, "from a non-session: None"),
    ):
        add("calendar.SessionCalendar.advance", why, {"calendar": cal_w, "session": session, "sessions": n})

    # ---------------------------------------------------------------- indicators
    d12 = weekdays(12)
    a12 = walk(1, "ALPHA", d12, seed=11)
    add("indicators.require_columns", "every required column present", {"bars": bars_frame(a12)})
    add("indicators.require_columns", "the optional columns may be absent",
        {"bars": bars_frame(a12, ("instrument_id", "date", "open", "high", "low", "close",
                                  "close_raw", "volume"))})  # fmt: skip
    add("indicators.require_columns", "close_raw and volume missing: ValueError names both",
        {"bars": bars_frame(a12, ("instrument_id", "date", "open", "high", "low", "close"))})  # fmt: skip

    b12 = walk(2, "BETA", d12, seed=12, holes={3, 4, 9})
    cal12 = make_cal(d12)
    two = bars_frame(a12 + b12)
    add("indicators.densify", "one name prints every session: unchanged shape", {"bars": bars_frame(a12), "calendar": cal12})
    add("indicators.densify", "BETA misses three sessions: null rows, symbol carried",
        {"bars": two, "calendar": cal12})  # fmt: skip
    add("indicators.densify", "without a symbol column", {
        "bars": bars_frame(a12 + b12, ("instrument_id", "date", "open", "high", "low", "close",
                                       "close_raw", "volume")),
        "calendar": cal12})  # fmt: skip
    add("indicators.densify", "a calendar shorter than the bars: rows outside it are dropped",
        {"bars": two, "calendar": make_cal(d12[2:8])})  # fmt: skip
    add("indicators.densify", "a calendar with a session no name printed: all-null rows",
        {"bars": bars_frame(a12[:6]), "calendar": make_cal(d12[:7])})  # fmt: skip

    d40 = weekdays(40)
    alpha = walk(1, "ALPHA", d40, seed=21, spikes={30: (0.08, 8.0)})
    beta = walk(2, "BETA", d40, seed=22, drift=-0.003, holes={5, 6, 25})
    gamma = walk(3, "GAMMA", d40, seed=23, start=28.0, volume=40_000.0,
                 spikes={33: (0.07, 9.0)}, circuit={33: 29.5, 34: 999.0})  # fmt: skip
    cal40 = make_cal(d40)
    add("indicators.with_vbt_indicators", "three names, compressed windows: a breakout, a "
        "downtrend with holes, a small-cap that prints at its upper band",
        {"bars": bars_frame(alpha + beta + gamma), "calendar": cal40, "config": small_json})  # fmt: skip
    add("indicators.with_vbt_indicators", "the default config over 40 sessions: every long "
        "window is still warming up (null)",
        {"bars": bars_frame(alpha), "calendar": cal40, "config": default_json})  # fmt: skip
    flat = [{**r, "open": 50.0, "high": 50.0, "low": 50.0, "close": 50.0, "close_raw": 50.0}
            for r in walk(4, "FLAT", d40[:15], seed=24)]  # fmt: skip
    add("indicators.with_vbt_indicators", "a locked flat bar: close_position is 0.5, change 0",
        {"bars": bars_frame(flat), "calendar": make_cal(d40[:15]), "config": small_json})  # fmt: skip
    add("indicators.with_vbt_indicators", "the optional columns absent: adj_factor 1.0, no circuit",
        {"bars": bars_frame(alpha[:20], ("instrument_id", "date", "open", "high", "low", "close",
                                         "close_raw", "volume")),
         "calendar": make_cal(d40[:20]), "config": small_json})  # fmt: skip
    split = walk(5, "SPLIT", d40[:25], seed=25, adj={k: 0.5 for k in range(12)})
    add("indicators.with_vbt_indicators", "an adjusted series before a 1:2 split: close_raw "
        "diverges from close, turnover reads the raw print",
        {"bars": bars_frame(split), "calendar": make_cal(d40[:25]), "config": small_json})  # fmt: skip
    add("indicators.with_vbt_indicators", "a missing required column raises",
        {"bars": bars_frame(alpha[:5], ("instrument_id", "date", "open", "close")),
         "calendar": make_cal(d40[:5]), "config": small_json})  # fmt: skip
    add("indicators.with_vbt_indicators", "BETA's holes under the default tolerance and a "
        "stricter rolling_min_share=1.0 (any hole blanks the window)",
        {"bars": bars_frame(beta), "calendar": cal40,
         "config": _cfg_json(small_config(data={"rolling_min_share": 1.0}))})  # fmt: skip

    # ---------------------------------------------------------------- signals (hand-built rows)
    scan_cases: list[tuple[str, list[dict[str, Any]]]] = [
        ("every line passes on the reference row", [{}]),
        ("line 1: volume exactly 3x vol_sma fails (strict >), a hair above passes",
         [{"volume": 300_000.0}, {"volume": 300_000.5}, {"volume": 299_999.0}]),
        ("line 2: close_raw exactly 30 fails (strict >), 30.05 passes; the adjusted close is "
         "not read", [{"close_raw": 30.0, "close": 60.0}, {"close_raw": 30.05}, {"close_raw": 29.0}]),
        ("line 3: change exactly 6.5% passes (>=), 6.49 fails",
         [{"change_pct": 6.5}, {"change_pct": 6.49}, {"change_pct": 20.0}]),
        ("line 4: vol_sma exactly 25,000 passes (>=), 24,999 fails",
         [{"vol_sma": 25_000.0, "volume": 80_000.0}, {"vol_sma": 24_999.0, "volume": 80_000.0}]),
        ("a null on any input is a fail, never a pass",
         [{"vol_sma": None}, {"change_pct": None}, {"close_raw": None}, {"volume": None}]),
        ("an empty frame", []),
    ]
    for why, rows in scan_cases:
        add("signals.chartink_scan", why, {"frame": indicated(rows), "config": default_json})
    add("signals.chartink_scan", "line 5 isolated (min_vol_sma lowered to 10,000 so line 4 "
        "passes): volume exactly 50,000 fails (strict >), 50,001 passes",
        {"frame": indicated([{"vol_sma": 16_000.0, "volume": 50_000.0},
                             {"vol_sma": 16_000.0, "volume": 50_001.0}]),
         "config": _cfg_json(with_config("scan", min_vol_sma=10_000.0))})  # fmt: skip
    add("signals.chartink_scan", "a recalibrated scan: vol_mult 2, min_change 5",
        {"frame": indicated([{"volume": 250_000.0, "change_pct": 5.0}]),
         "config": _cfg_json(with_config("scan", vol_mult=2.0, min_change_pct=5.0))})  # fmt: skip

    trend_cases: list[tuple[str, list[dict[str, Any]]]] = [
        ("every filter passes on the reference row", [{}]),
        ("A: close equal to the 200-DMA fails (strict >)",
         [{"sma_dma": 110.0}, {"sma_dma": 109.99}, {"sma_dma": None}]),
        ("B: close equal to the prior 20-session high fails (strict >)",
         [{"high_prior": 110.0}, {"high_prior": 109.99}, {"high_prior": None}]),
        ("C: a 20-day return of exactly 25% fails (strict <)",
         [{"ret_lookback_pct": 25.0}, {"ret_lookback_pct": 24.99}, {"ret_lookback_pct": -5.0},
          {"ret_lookback_pct": None}]),
        ("D: close position exactly 0.6 passes (>=), 0.59 fails",
         [{"close_position": 0.6}, {"close_position": 0.59}, {"close_position": None}]),
        ("E: change exactly 15% passes (<=), 15.01 fails",
         [{"change_pct": 15.0}, {"change_pct": 15.01}, {"change_pct": None}]),
        ("F: turnover exactly Rs 2 crore passes (>=)",
         [{"turnover_avg": 2e7}, {"turnover_avg": 19_999_999.0}, {"turnover_avg": None}]),
    ]
    for why, rows in trend_cases:
        add("signals.trend_filters", why, {"frame": indicated(rows), "config": default_json})

    mixed = indicated([
        {},  # SIGNAL
        {"sma_dma": 120.0, "high_prior": 111.0},  # SCAN_ONLY, failed A and B
        {"volume": 100.0},  # no scan hit: state null, failed_filters still listed
        {"ret_lookback_pct": 30.0, "close_position": 0.1, "change_pct": 16.0, "turnover_avg": 1.0},
        {"sma_dma": None, "high_prior": None, "ret_lookback_pct": None, "close_position": None,
         "turnover_avg": None},
    ])  # fmt: skip
    add("signals.with_signal_columns", "SIGNAL, SCAN_ONLY with failed letters, a non-hit, every "
        "filter failing, and a warming-up row whose nulls fail", {"frame": mixed, "config": default_json})
    add("signals.with_signal_columns", "an empty frame", {"frame": indicated([]), "config": default_json})
    add("signals.with_signal_columns", "a looser trend config turns SCAN_ONLY into SIGNAL",
        {"frame": mixed, "config": _cfg_json(with_config(
            "trend", max_ret_20_pct=40.0, min_close_position=0.0, max_change_pct=20.0,
            min_turnover_avg_inr=0.0))})  # fmt: skip

    jun3, jun4 = dt.date(2024, 6, 3), dt.date(2024, 6, 4)
    ranked = indicated([
        {"instrument_id": 7, "date": jun4, "turnover_inr": 5e7},
        {"instrument_id": 3, "date": jun3, "turnover_inr": 4e7},
        {"instrument_id": 5, "date": jun3, "turnover_inr": 9e7, "sma_dma": 200.0},
        {"instrument_id": 2, "date": jun3, "turnover_inr": 4e7},  # ties instrument 3
        {"instrument_id": 9, "date": jun3, "volume": 10.0},  # not a scan hit: absent
        {"instrument_id": 4, "date": jun4, "turnover_inr": None},  # a null rank key
    ])  # fmt: skip
    add("signals.detect_signals", "the whole history: by date, turnover descending, a tie broken by "
        "instrument_id ascending, SCAN_ONLY rows kept, non-hits dropped",
        {"frame": ranked, "as_of": None, "config": default_json})  # fmt: skip
    add("signals.detect_signals", "as_of restricts to one session",
        {"frame": ranked, "as_of": jun3, "config": default_json})  # fmt: skip
    add("signals.detect_signals", "as_of a session with a null rank key: polars sorts the null "
        "first even when descending (nulls_last=False)",
        {"frame": ranked, "as_of": jun4, "config": default_json})  # fmt: skip
    add("signals.detect_signals", "as_of a session with no rows: empty",
        {"frame": ranked, "as_of": dt.date(2024, 6, 5), "config": default_json})  # fmt: skip
    add("signals.detect_signals", "an empty frame", {"frame": indicated([]), "as_of": None, "config": default_json})

    add("signals.signal_mask", "one boolean per row; SCAN_ONLY and non-hits are False",
        {"frame": mixed, "config": default_json})  # fmt: skip
    add("signals.signal_mask", "ranked frame", {"frame": ranked, "config": default_json})
    add("signals.signal_mask", "an empty frame", {"frame": indicated([]), "config": default_json})

    # Signals over a real indicated frame: the pipeline the nightly runs.
    from baskfy_core.vbt.calendar import build_calendar
    from baskfy_core.vbt.indicators import with_vbt_indicators
    from baskfy_core.vbt.signals import with_signal_columns

    pipe_bars = bars_frame(alpha + beta + gamma)
    pipe = with_vbt_indicators(pipe_bars, build_calendar(pipe_bars, small), small)
    add("signals.detect_signals", "over with_vbt_indicators' own output (compressed config)",
        {"frame": pipe, "as_of": None, "config": small_json})  # fmt: skip
    add("signals.with_signal_columns", "over with_vbt_indicators' own output (compressed config)",
        {"frame": pipe.filter(pl.col("date") >= d40[28]), "config": small_json})  # fmt: skip

    # ---------------------------------------------------------------- breadth
    for pct, breadth, why in (
        ("40.0000", None, "exactly 40% is SHUT (strict >)"),
        ("40.0001", None, "a hair above is OPEN"),
        ("0.0000", None, "nothing above: SHUT"),
        ("100.0000", None, "everything above: OPEN"),
        ("39.9999", None, "a hair below: SHUT"),
        ("35.5", {"dma_bars": 200, "min_pct_above_dma": 35.0}, "a recalibrated 35% threshold"),
        ("35.0", {"dma_bars": 200, "min_pct_above_dma": 35.0}, "exactly the recalibrated threshold"),
    ):
        add("breadth.gate_for", why, {"pct_above_dma": pct, "breadth": breadth})

    def breadth_frame(rows: list[tuple[int, dt.date, float | None, float | None]]) -> pl.DataFrame:
        return pl.DataFrame(rows, schema={"instrument_id": pl.Int64, "date": pl.Date,
                                          "close": pl.Float64, "sma_dma": pl.Float64},
                            orient="row")  # fmt: skip

    b1, b2, b3 = dt.date(2024, 6, 3), dt.date(2024, 6, 4), dt.date(2024, 6, 5)
    bf = breadth_frame([
        (1, b1, 110.0, 100.0), (2, b1, 90.0, 100.0), (3, b1, 100.0, 100.0),  # a tie is not above
        (4, b1, 50.0, None),  # warming up: in neither count
        (5, b1, None, 80.0),  # no bar: not in the universe
        (1, b2, 110.0, 100.0), (2, b2, 101.0, 100.0), (3, b2, 99.0, 100.0),
        (1, b3, 120.0, None), (2, b3, 120.0, None),  # nobody measured
    ])  # fmt: skip
    add("breadth.breadth_above_dma", "one session: a tie is not above, a warming-up name and a "
        "blank bar are in neither count", {"frame": bf, "as_of": b1, "config": default_json})
    add("breadth.breadth_above_dma", "two of three above: 66.6667%, OPEN",
        {"frame": bf, "as_of": b2, "config": default_json})  # fmt: skip
    add("breadth.breadth_above_dma", "nobody has a 200-DMA: 0.0000 and SHUT, not a division by zero",
        {"frame": bf, "as_of": b3, "config": default_json})  # fmt: skip
    add("breadth.breadth_above_dma", "as_of None reads the whole frame and stamps its latest date",
        {"frame": bf.filter(pl.col("date") == b2), "as_of": None, "config": default_json})  # fmt: skip
    add("breadth.breadth_above_dma", "an empty frame with as_of None: session null",
        {"frame": breadth_frame([]), "as_of": None, "config": default_json})  # fmt: skip
    add("breadth.breadth_above_dma", "as_of a date the frame lacks: zero counts, session stamped",
        {"frame": bf, "as_of": dt.date(2024, 6, 6), "config": default_json})  # fmt: skip
    add("breadth.breadth_above_dma", "exactly 40% under a recalibrated 39.99 threshold: OPEN",
        {"frame": breadth_frame([(i, b1, 110.0 if i <= 2 else 90.0, 100.0) for i in range(1, 6)]),
         "as_of": b1, "config": _cfg_json(with_config("breadth", min_pct_above_dma=39.99))})  # fmt: skip
    add("breadth.breadth_above_dma", "exactly 40% under the default: SHUT",
        {"frame": breadth_frame([(i, b1, 110.0 if i <= 2 else 90.0, 100.0) for i in range(1, 6)]),
         "as_of": b1, "config": default_json})  # fmt: skip

    add("breadth.breadth_series", "three sessions, one per row, sorted; a session with nobody "
        "measured is 0.0 and SHUT", {"frame": bf, "config": default_json})
    add("breadth.breadth_series", "an empty frame", {"frame": breadth_frame([]), "config": default_json})
    add("breadth.breadth_series", "over with_vbt_indicators' output (compressed config, 20-DMA)",
        {"frame": pipe.select("instrument_id", "date", "close", "sma_dma"), "config": small_json})  # fmt: skip
    add("breadth.breadth_series", "a session at exactly the threshold is SHUT",
        {"frame": breadth_frame([(i, b1, 110.0 if i <= 2 else 90.0, 100.0) for i in range(1, 6)]),
         "config": default_json})  # fmt: skip

    # ---------------------------------------------------------------- sizing
    from baskfy_core.vbt.config import SizingConfig

    sz = _cfg_json(SizingConfig())

    def size(why: str, **kw: Any) -> None:
        base = {"equity": "1000000", "cash_available": "1000000", "limit_price": "100.00",
                "stop_price": "88.00", "turnover_avg_inr": "500000000", "config": sz,
                "slot_multiplier": "1"}  # fmt: skip
        base.update(kw)
        add("sizing.size_entry", why, base)

    size("the ordinary answer: a tenth of equity, SLOT binds")
    size("POSITION_PCT binds when the slot is above 12.5% (six slots)",
         config={**sz, "max_slots": 6})  # fmt: skip
    size("CASH binds: the sleeve has Rs 40,000 left", cash_available="40000")
    size("TURNOVER binds: 1% of a Rs 50 lakh name", turnover_avg_inr="5000000")
    size("no turnover reading: three budgets", turnover_avg_inr=None)
    size("half a slot on the first live sessions", slot_multiplier="0.5")
    size("NO_SLEEVE_CAPITAL: equity zero", equity="0")
    size("NO_SLEEVE_CAPITAL: equity negative", equity="-5")
    size("STOP_NOT_BELOW_ENTRY: stop equal to the limit", stop_price="100.00")
    size("STOP_NOT_BELOW_ENTRY: a zero limit", limit_price="0", stop_price="-1")
    size("BELOW_MIN_TRADE_VALUE: Rs 9,990 of cash buys 29 shares, Rs 9,667.15", cash_available="9990", limit_price="333.35",
         stop_price="293.30")  # fmt: skip
    size("exactly the minimum trade value is placed", cash_available="10000")
    size("TURNOVER_CAP: the turnover ceiling cannot fund the minimum", turnover_avg_inr="900000")
    size("negative cash is clamped to zero: BELOW_MIN_TRADE_VALUE", cash_available="-500")
    size("a price that does not divide the budget: the floor, and the paise rounding of risk",
         limit_price="137.45", stop_price="120.95", equity="987654.32", cash_available="987654.32")  # fmt: skip

    for left, enabled, why in (
        (5, True, "live and counting down: half"),
        (1, True, "the last first-live session: still half"),
        (0, True, "the countdown is done: full"),
        (5, False, "paper: full size whatever the countdown"),
        (-1, True, "a negative countdown is done"),
    ):
        add("sizing.first_live_multiplier", why,
            {"sessions_left": left, "execution_enabled": enabled, "config": sz})  # fmt: skip
    add("sizing.first_live_multiplier", "a recalibrated 0.25 multiplier",
        {"sessions_left": 3, "execution_enabled": True,
         "config": {**sz, "risk_multiplier_first_live": 0.25}})  # fmt: skip

    # ---------------------------------------------------------------- exits
    for price, tick, why in (
        ("88.04", "0.05", "down to 88.00"),
        ("88.05", "0.05", "on the tick: unchanged"),
        ("88.0999", "0.05", "never up: 88.05"),
        ("87.999", "0.01", "the paise tick"),
        ("-1.03", "0.05", "a negative price rounds toward zero (ROUND_DOWN), -1.00"),
        ("0.04", "0.05", "below one tick: 0.00"),
    ):
        add("exits.tick_floor", why, {"price": price, "tick": tick})

    for fill, tick, cfg, why in (
        ("100.00", "0.05", default_json, "12% below a round fill: 88.00"),
        ("123.45", "0.05", default_json, "108.636 floors to 108.60"),
        ("123.45", "0.01", default_json, "the paise tick: 108.63"),
        ("31.10", "0.05", default_json, "a low-priced fill: 27.368 -> 27.35"),
        ("100.00", "0.05", _cfg_json(with_config("exits", stop_pct=10.0)), "a 10% stop"),
        ("2499.95", "0.05", default_json, "a four-digit fill"),
    ):
        add("exits.initial_stop", why, {"fill_price": fill, "tick": tick, "config": cfg})

    for cur, prop, why in (
        ("88.00", "90.00", "a higher stop is taken"),
        ("88.00", "85.00", "a stop never falls"),
        ("88.00", "88.00", "equal"),
        ("88.00", "88.000", "equal value, different exponent: the current is kept"),
    ):
        add("exits.apply_stop", why, {"current": cur, "proposed": prop})

    def bar(session: dt.date = dt.date(2024, 6, 10), o: str | None = "100", h: str | None = "102",
            lo: str | None = "99", c: str | None = "101", ema: str | None = "98") -> dict[str, Any]:  # fmt: skip
        return {"session": session, "open": o, "high": h, "low": lo, "close": c, "ema_exit": ema,
                "history": []}  # fmt: skip

    for b, stop, why in (
        (bar(), "88", "untouched: None"),
        (bar(o="87", lo="85"), "88", "gapped through at the open: STOP_GAP at the open"),
        (bar(o="88", lo="87"), "88", "an open exactly at the stop is a gap: STOP_GAP"),
        (bar(lo="87.5"), "88", "touched intraday: STOP_HIT at the stop"),
        (bar(lo="88"), "88", "a low exactly at the stop: STOP_HIT"),
        (bar(o=None, lo="80"), "88", "no open, low through: STOP_HIT"),
        (bar(o=None, h=None, lo=None, c=None, ema=None), "88", "no bar at all: None"),
    ):
        add("exits.stop_fill", why, {"bar": b, "stop": stop})

    pos = {"instrument_id": 1, "entry_date": dt.date(2024, 6, 3), "entry_price": "100.00",
           "quantity": 100, "stop_price": "88.00", "initial_stop": "88.00", "exit_queued_for": None}  # fmt: skip
    nobar = bar(o=None, h=None, lo=None, c=None, ema=None)
    for b, blanks, why in (
        (bar(), 0, "above the EMA, stop intact: HOLD"),
        (nobar, 0, "no bar today: HOLD"),
        (nobar, 4, "four blank sessions: still HOLD"),
        (nobar, 5, "five blank sessions: WRITE_OFF, NO_BAR"),
        (nobar, 9, "well past the tolerance: WRITE_OFF"),
        (bar(o="86", lo="85", c="87"), 0, "gapped below the stop: STOPPED_OUT, STOP_GAP"),
        (bar(lo="87", c="95", ema="96"), 0, "touched the stop and closed under the EMA: the stop "
                                            "outranks the EMA"),
        (bar(c="97.5", ema="98"), 0, "closed below the EMA: QUEUE_SELL_AT_OPEN, EMA_EXIT"),
        (bar(c="98", ema="98"), 0, "a close equal to the EMA holds (strict <)"),
        (bar(lo="89", c="90", ema=None), 0, "the EMA is warming up: held, never sold on an "
                                             "absence"),
    ):
        add("exits.manage", why, {"position": pos, "bar": b, "blank_sessions": blanks, "config": default_json})
    add("exits.manage", "a recalibrated no-bar tolerance of 2 and a 10-EMA note",
        {"position": pos, "bar": nobar, "blank_sessions": 2,
         "config": _cfg_json(with_config("exits", no_bar_tolerance_sessions=2, trail_ema_bars=10))})  # fmt: skip
    add("exits.manage", "the note names the configured EMA span",
        {"position": pos, "bar": bar(c="97.5", ema="98"), "blank_sessions": 0,
         "config": _cfg_json(with_config("exits", trail_ema_bars=10))})  # fmt: skip

    for entry, initial, exit_price, why in (
        ("100", "88", "124", "+2R"),
        ("100", "88", "82", "-1.5R"),
        ("100", "88", "100", "flat: 0.00"),
        ("100", "100", "110", "zero risk: None"),
        ("100", "105", "110", "negative risk: None"),
        ("123.45", "108.63", "131.07", "rounded half-even to 2 dp"),
    ):
        add("exits.r_multiple", why, {"entry": entry, "initial": initial, "exit_price": exit_price})

    # ---------------------------------------------------------------- orders
    oweek = [dt.date(2024, 1, d) for d in (1, 2, 3, 4, 5, 8, 9, 10, 11, 12)]
    ocal = make_cal(oweek)
    ocal_holiday = make_cal([d for d in oweek if d != dt.date(2024, 1, 4)])
    entry_short = _cfg_json(with_config("entry", valid_sessions=2))
    for sig, cal, cfg, why in (
        (dt.date(2024, 1, 2), ocal, default_json, "three sessions after Tuesday: Friday"),
        (dt.date(2024, 1, 3), ocal, default_json, "across the weekend: Monday"),
        (dt.date(2024, 1, 2), ocal_holiday, default_json, "a holiday does not count: Monday"),
        (dt.date(2024, 1, 10), ocal, default_json, "the calendar ends first: None"),
        (dt.date(2024, 1, 9), ocal, default_json, "exactly the last session"),
        (dt.date(2024, 1, 2), ocal, entry_short, "a two-session window"),
        (dt.date(2024, 1, 6), ocal, default_json, "a signal on a non-session: None"),
    ):
        add("orders.expires_after", why, {"signal_date": sig, "calendar": cal, "config": cfg})

    for sig, ses, cal, why in (
        (dt.date(2024, 1, 2), dt.date(2024, 1, 5), ocal, "three sessions"),
        (dt.date(2024, 1, 2), dt.date(2024, 1, 5), ocal_holiday, "the holiday counts for none: two"),
        (dt.date(2024, 1, 2), dt.date(2024, 1, 2), ocal, "the signal session itself: 0"),
        (dt.date(2024, 1, 2), dt.date(2024, 1, 4), ocal_holiday, "the session is a holiday: 0"),
        (dt.date(2024, 1, 5), dt.date(2024, 1, 2), ocal, "before the signal: negative"),
    ):
        add("orders.sessions_since", why, {"signal_date": sig, "session": ses, "calendar": cal})

    for ses, cal, cfg, why in (
        (dt.date(2024, 1, 4), ocal, default_json, "t+2: still working"),
        (dt.date(2024, 1, 5), ocal, default_json, "the close of t+3: expired (>=)"),
        (dt.date(2024, 1, 8), ocal, default_json, "t+4: expired"),
        (dt.date(2024, 1, 5), ocal_holiday, default_json, "a holiday inside the window: t+2 only"),
        (dt.date(2024, 1, 4), ocal, entry_short, "a two-session window expires at t+2"),
        (dt.date(2024, 1, 6), ocal, default_json, "a non-session: 0 sessions, not expired"),
    ):
        add("orders.is_expired", why, {"order": order_json(), "session": ses, "calendar": cal, "config": cfg})

    add("orders.advance_session", "a fresh order works its first session", {"order": order_json()})
    add("orders.advance_session", "a partially filled order keeps its fill",
        {"order": order_json(state="PARTIAL", sessions_worked=2, filled=40)})  # fmt: skip
    add("orders.advance_session", "an over-filled order: remaining clamps to zero",
        {"order": order_json(state="FILLED", sessions_worked=1, filled=120)})  # fmt: skip

    mix = [
        order_json(1, state="PROPOSED"),
        order_json(2, state="CONFIRMED"),
        order_json(3, state="SENT"),
        order_json(4, state="PARTIAL", filled=30),
        order_json(5, state="FILLED", filled=100),  # terminal: dropped from both lists
        order_json(6, state="CANCELLED", cancel_reason="MANUAL"),
        order_json(7, signal_date=dt.date(2024, 1, 4), state="SENT"),  # younger: still live
    ]
    for ses, reason, why in (
        (dt.date(2024, 1, 5), "EXPIRY_SWEEP", "at t+3: unsent orders EXPIRED, held ones CANCELLED, "
                                              "a partial cancelled for its remainder, terminal "
                                              "orders in neither list"),
        (dt.date(2024, 1, 4), "EXPIRY_SWEEP", "at t+2: everything live stays live"),
        (dt.date(2024, 1, 5), "GATE_SHUT", "a caller-supplied reason is stamped"),
        (dt.date(2024, 1, 9), "SLOT_TAKEN", "late enough that the younger order expires too"),
    ):
        add("orders.expire_orders", why,
            {"orders": mix, "session": ses, "calendar": ocal, "config": default_json, "reason": reason})  # fmt: skip
    add("orders.expire_orders", "no orders", {"orders": [], "session": dt.date(2024, 1, 5),
                                              "calendar": ocal, "config": default_json,
                                              "reason": "EXPIRY_SWEEP"})  # fmt: skip

    through = _cfg_json(with_config("entry", fill_through_pct=0.25))
    for kw, cfg, why in (
        ({"open_price": "101", "high": "102", "low": "99.5"}, default_json,
         "the low trades through: filled at the limit"),
        ({"open_price": "98", "high": "99", "low": "97"}, default_json,
         "a gap-down open fills below the bid, at the open"),
        ({"open_price": "101", "high": "102", "low": "100"}, default_json,
         "a low exactly at the limit fills"),
        ({"open_price": "101", "high": "102", "low": "100.01"}, default_json,
         "a low a paisa above: no fill"),
        ({"open_price": "100", "high": "100", "low": "100"}, default_json,
         "a locked bar (open = high = low): no fill"),
        ({"open_price": None, "high": "102", "low": "99"}, default_json, "no open: no fill"),
        ({"open_price": "0", "high": "102", "low": "99"}, default_json, "a zero open: no fill"),
        ({"open_price": "101", "high": "102", "low": "99.80"}, through,
         "fill_through 0.25%: 99.80 is not through 99.75"),
        ({"open_price": "101", "high": "102", "low": "99.75"}, through,
         "fill_through 0.25%: 99.75 is"),
    ):
        add("orders.fill_if_touched", why, {"order": order_json(), "session": dt.date(2024, 1, 3),
                                            **kw, "config": cfg})  # fmt: skip
    add("orders.fill_if_touched", "a partial fills its remainder",
        {"order": order_json(state="PARTIAL", filled=60), "session": dt.date(2024, 1, 3),
         "open_price": "101", "high": "102", "low": "99", "config": default_json})  # fmt: skip
    add("orders.fill_if_touched", "nothing remaining: None",
        {"order": order_json(state="FILLED", filled=100), "session": dt.date(2024, 1, 3),
         "open_price": "101", "high": "102", "low": "99", "config": default_json})  # fmt: skip

    for order, qty, why in (
        (order_json(), 100, "the whole quantity: FILLED"),
        (order_json(), 40, "some of it: PARTIAL"),
        (order_json(state="PARTIAL", filled=40), 60, "the rest: FILLED"),
        (order_json(state="PARTIAL", filled=40), 80, "more than the rest: FILLED, over-filled"),
    ):
        add("orders.apply_fill", why, {"order": order, "fill": {"quantity": qty, "price": "99.50",
                                                                "session": dt.date(2024, 1, 3)}})  # fmt: skip

    # ---------------------------------------------------------------- sleeve
    def opv(iid: int, q: int, entry: str, mark: str) -> dict[str, Any]:
        return {"instrument_id": iid, "quantity_open": q, "entry_avg": entry, "mark": mark}

    def wc(iid: int, q: int, limit: str) -> dict[str, Any]:
        return {"instrument_id": iid, "quantity": q, "limit_price": limit}

    for kw, why in (
        ({"capital_inr": "1000000", "realised_inr": "0", "open_positions": [], "working_orders": []},
         "a fresh sleeve: all cash"),
        ({"capital_inr": "1000000", "realised_inr": "12500.50",
          "open_positions": [opv(1, 100, "500.25", "520.10"), opv(2, 40, "1200", "1180.55")],
          "working_orders": [wc(3, 150, "640.35")]},
         "two open, one resting limit: cash, committed, equity, exposure, unrealised"),
        ({"capital_inr": "100000", "realised_inr": "-5000",
          "open_positions": [opv(1, 100, "500", "450")], "working_orders": [wc(2, 100, "600")]},
         "committed beyond cash: cash_available clamps to zero"),
        ({"capital_inr": "0", "realised_inr": "0", "open_positions": [], "working_orders": []},
         "no capital"),
        ({"capital_inr": "500000", "realised_inr": "0",
          "open_positions": [opv(1, 33, "123.456", "123.454")],
          "working_orders": [wc(2, 7, "99.995")]},
         "sub-paise amounts: quantize rounds half-even to the paise"),
        ({"capital_inr": "1000000", "realised_inr": "250000.125",
          "open_positions": [opv(i, 10 * i, f"{100 + i}.50", f"{110 + i}.25") for i in range(1, 6)],
          "working_orders": []},
         "five positions in profit, a fractional realised figure"),
        ({"capital_inr": "1000000", "realised_inr": "0", "open_positions": [],
          "working_orders": [wc(1, 100, "100"), wc(2, 200, "50"), wc(3, 0, "75")]},
         "only working orders, one of zero quantity"),
    ):
        add("sleeve.sleeve_value", why, kw)

    # ---------------------------------------------------------------- plan
    for price, tick, why in (
        ("100.02", "0.05", "down to 100.00"),
        ("100.025", "0.05", "exactly half a tick rounds up (ROUND_HALF_UP): 100.05"),
        ("100.03", "0.05", "up to 100.05"),
        ("100.07", None, "tick None is the exchange's 0.05"),
        ("99.999", "0.01", "the paise tick"),
        ("0.02", "0.05", "below half a tick: 0.00"),
        ("0.025", "0.05", "half a tick: 0.05"),
        ("123.4567", "0.10", "a ten-paise tick"),
    ):
        add("plan.to_tick", why, {"price": price, "tick": tick})

    for book, ids, why in (
        (book_json(), [1], "an empty book: nothing held, no slots"),
        (book_json(open_instrument_ids=[1, 2], working_instrument_ids=[3]), [1, 3, 4],
         "no entry counts: one per held name, working orders take slots"),
        (book_json(open_instrument_ids=[1, 2], open_entry_counts={1: 2, 2: 1},
                   working_instrument_ids=[5, 6]), [1, 2, 5],
         "pyramided: the counts win, a second entry is a second slot"),
        (book_json(open_instrument_ids=[1], open_entry_counts={9: 3}), [1, 9],
         "counts present: a held id missing from them reads 0"),
        (book_json(working_instrument_ids=[1, 2, 3]), [1], "only working orders"),
    ):
        add("plan.BookState", why, {"book": book, "instrument_ids": ids})

    trio = [cand(1, "AAA", "250.00", 9_000), cand(2, "BBB", "120.40", 7_000),
            cand(3, "CCC", "64.85", 8_000)]  # fmt: skip
    no_pyr = _cfg_json(with_config("sizing", pyramiding=False))

    def entries(why: str, **kw: Any) -> None:
        base = {"candidates": trio, "gate": "OPEN", "equity": "1000000", "book": book_json(),
                "config": default_json, "slot_multiplier": "1", "max_open_positions": None,
                "tick": None}  # fmt: skip
        base.update(kw)
        add("plan.build_entries", why, base)

    entries("three lines, ranked by signal-day turnover, cash spent once")
    entries("the gate is SHUT: every candidate GATE_SHUT", gate="SHUT")
    entries("no sleeve capital: NO_SLEEVE_CAPITAL before the gate", equity="0", gate="SHUT")
    entries("a locked upper circuit is skipped, the rest are lined",
            candidates=[cand(1, "AAA", "250.00", 9_000, locked=True), *trio[1:]])  # fmt: skip
    entries("pyramiding off: a held name is ALREADY_HELD", config=no_pyr,
            book=book_json(open_instrument_ids=[1]))  # fmt: skip
    entries("pyramiding on: one open entry allows a second, two is the cap",
            book=book_json(open_instrument_ids=[1, 2], open_entry_counts={1: 1, 2: 2}))  # fmt: skip
    entries("a resting limit: ALREADY_WORKING", book=book_json(working_instrument_ids=[3]))
    entries("two entries already this session: one line, then SESSION_CAP",
            book=book_json(entries_already_this_session=2))  # fmt: skip
    entries("nine slots taken: one line, then SLOTS_FULL",
            book=book_json(open_instrument_ids=list(range(11, 19)), working_instrument_ids=[30]))  # fmt: skip
    entries("the trader's cap of 2 under the strategy's 10", max_open_positions=2)
    entries("a cap above 10 never exceeds the strategy's ten",
            max_open_positions=50, book=book_json(open_instrument_ids=list(range(11, 20))))  # fmt: skip
    entries("Rs 1,50,000 of cash: a slot, then CASH binds, then BELOW_MIN_TRADE_VALUE",
            book=book_json(cash_available_inr="150000"))  # fmt: skip
    entries("a thin name: TURNOVER binds; a thinner one: TURNOVER_CAP",
            candidates=[cand(1, "AAA", "250.00", 9_000, turnover="3000000"),
                        cand(2, "BBB", "120.40", 7_000, turnover="900000"),
                        cand(3, "CCC", "64.85", 8_000, turnover=None)])  # fmt: skip
    entries("a sub-tick close: the limit rounds to 0.00 and the stop is not below it",
            candidates=[cand(1, "PENNY", "0.02", 9_000)])  # fmt: skip
    entries("exposure already at 95%: EXPOSURE_FULL",
            book=book_json(open_exposure_inr="950000"))  # fmt: skip
    entries("half a slot, a ten-paise tick, and a rank tie broken by symbol",
            candidates=[cand(4, "ZED", "333.33", 5_000), cand(5, "ACE", "77.77", 5_000)],
            slot_multiplier="0.5", tick="0.10")  # fmt: skip
    entries("no candidates", candidates=[])

    sell = act("QUEUE_SELL_AT_OPEN", "EMA_EXIT", note="close 97.5 below the 21-day EMA 98")
    partial = act("QUEUE_PARTIAL_AT_OPEN", "PARTIAL", note="a third into strength", quantity=33,
                  new_stop="100.25")  # fmt: skip
    for managed, expired, book, why in (
        ([[1, "AAA", 100, sell]], [], book_json(), "a queued EMA exit: SELL_AT_OPEN, no position id"),
        ([[1, "AAA", 100, sell, 501]], [], book_json(), "the fifth element is kept as position_id"),
        ([[2, "BBB", 99, partial, 502]], [], book_json(),
         "a partial with a breakeven: the sale and a RAISE_GTT_STOP for the 66 left"),
        ([[2, "BBB", 99, {**partial, "new_stop": None}]], [], book_json(),
         "a partial without a new stop: the sale alone"),
        ([[2, "BBB", 99, {**partial, "quantity": 0}], [3, "CCC", 50, {**partial, "quantity": None}]],
         [], book_json(), "a partial of zero or no shares: no line"),
        ([[4, "DDD", 70, act("RAISE_STOP", note="to breakeven", new_stop="210.00"), 504],
          [5, "EEE", 70, act("RAISE_STOP", note="nothing to raise to")]], [], book_json(),
         "a raise with a stop is a line; one without is not"),
        ([[6, "FFF", 10, act("HOLD")], [7, "GGG", 10, act("STOPPED_OUT", "STOP_HIT", "88.00")],
          [8, "HHH", 10, act("WRITE_OFF", "NO_BAR")]], [], book_json(),
         "hold, stopped-out and write-off make no line (the GTT and the alert own them)"),
        ([], [[order_json(9, state="SENT"), "III"],
              [order_json(10, state="PARTIAL", filled=70, limit="55.55"), "JJJ"]],
         book_json(positions_naked_of_gtt=[[11, "KKK", 25, "301.15"], [12, "LLL", 5, "99.95"]]),
         "expired limits are cancelled for their remainder; naked positions get an ARM_GTT"),
        ([[1, "AAA", 100, sell], [2, "BBB", 99, partial]],
         [[order_json(9, state="SENT"), "III"]],
         book_json(positions_naked_of_gtt=[[11, "KKK", 25, "301.15"]]),
         "every kind at once, in exit_lines' own order"),
        ([], [], book_json(), "nothing to do"),
    ):
        add("plan.exit_lines", why, {"managed": managed, "expired": expired, "book": book})

    entry_a = line("PLACE_LIMIT", 1, "AAA", 400, limit="250.00", stop="220.00",
                   value="100000.00", cap="SLOT", note="limit at the signal close; slot bound the size")  # fmt: skip
    entry_b = line("PLACE_LIMIT", 2, "BBB", 830, limit="120.40", stop="105.95",
                   value="99932.00", cap="SLOT", note="limit at the signal close; slot bound the size")  # fmt: skip
    market = line("BUY_AT_MARKET", 3, "CCC", 100, limit="64.85", stop="57.05", value="6485.00",
                  cap="CASH")  # fmt: skip
    sell_l = line("SELL_AT_OPEN", 4, "DDD", 70, reason="EMA_EXIT", note="close below the EMA")
    sell_p = line("SELL_AT_OPEN", 5, "EEE", 33, reason="PARTIAL", position_id=55)
    raise_l = line("RAISE_GTT_STOP", 5, "EEE", 66, stop="100.25", position_id=55)
    cancel = line("CANCEL_LIMIT", 6, "FFF", 100, limit="100.00")
    arm = line("ARM_GTT", 7, "GGG", 25, stop="301.15")
    skips = [{"instrument_id": 8, "symbol": "HHH", "reason": "SESSION_CAP",
              "detail": "3 new entries a session"}]  # fmt: skip
    session = dt.date(2026, 10, 27)
    for exits_, entries_, skips_, gate, source, why in (
        ([sell_l, cancel], [entry_b, entry_a], skips, "OPEN", "EVENING",
         "exits first, entries after, each kind sorted by symbol"),
        ([arm, raise_l, sell_p, cancel, sell_l], [market, entry_a], [], "OPEN", "LIVE",
         "every kind: LINE_ORDER decides, symbol breaks the tie; a live market buy counts as an "
         "entry"),
        ([], [], [], "SHUT", "EVENING", "an empty plan has a hash too"),
        ([], [entry_a, entry_b], [], "OPEN", "EVENING", "the same lines in another order hash "
                                                        "the same"),
        ([sell_l], [], skips, "SHUT", "EVENING", "exits only, the gate shut"),
    ):
        add("plan.assemble", why, {"session": session, "gate": gate, "equity": "1000000",
                                   "exits": exits_, "entries": entries_, "skips": skips_,
                                   "source": source})  # fmt: skip

    # ---------------------------------------------------------------- drift / published
    for cagr, dd, trades, study, threshold, why in (
        (18.23, -27.94, 761, None, None, "the study itself: zero deltas, not flagged"),
        (19.23, -27.94, 761, None, None, "exactly one point above: not flagged (strict >)"),
        (19.24, -26.0, 770, None, None, "a hair over a point above: flagged"),
        (14.83, -33.12, 701, None, None, "3.4 points below: flagged, signed negative"),
        (17.5, -28.0, 761, None, 0.5, "a tighter threshold flags 0.73"),
        (12.0, -20.0, 100, {"start": "2020-01-01", "end": "2021-01-01", "years": 1.0,
                            "cagr_pct": 12.5, "max_drawdown_pct": -18.25, "trades": 90,
                            "win_rate_pct": 40.0, "profit_factor": 1.2, "avg_hold_sessions": 10.0,
                            "exposure_pct": 50.0, "sharpe": 0.8, "in_sample_cagr_pct": 10.0,
                            "in_sample_dd_pct": -15.0, "out_of_sample_cagr_pct": 15.0,
                            "out_of_sample_dd_pct": -18.25, "modelled_fill_rate_pct": 80.0},
         None, "against another study"),
        (18.234567, -27.941234, 761, None, None, "float deltas rounded to 2 dp"),
    ):
        add("drift.compare", why, {"cagr_pct": cagr, "max_drawdown_pct": dd, "trades": trades,
                                   "study": study, "threshold": threshold})  # fmt: skip
    add("published.PUBLISHED", "the study's record and its Decimal headline", {})

    # ---------------------------------------------------------------- backtest
    pdays = weekdays(6, dt.date(2024, 6, 3))
    pschema = {"instrument_id": pl.Int64, "symbol": pl.String, "date": pl.Date,
               "open": pl.Float64, "high": pl.Float64, "low": pl.Float64, "close": pl.Float64,
               "ema_exit": pl.Float64, "turnover_avg": pl.Float64, "turnover_inr": pl.Float64,
               "state": pl.String}  # fmt: skip
    prow = []
    for k, d in enumerate(pdays):
        for iid, sym in ((20, "ZETA"), (10, "ALPHA")):
            if iid == 20 and k == 2:
                continue  # ZETA missing a session: NaN in every matrix
            price = 100.0 + k + iid / 10
            state = "SIGNAL" if (iid, k) == (10, 1) else "SCAN_ONLY" if (iid, k) == (20, 3) else None
            prow.append([iid, sym if k else None, d, price, price + 1, price - 1, price + 0.5,
                         price - 2, 3e7, 4e7, state])  # fmt: skip
    pframe = pl.DataFrame(prow, schema=pschema, orient="row").sample(fraction=1.0, shuffle=True, seed=3)
    bool_frame = pframe.with_columns((pl.col("state") == "SCAN_ONLY").alias("hit")).drop("state")
    probes = [pdays[0], pdays[3], dt.date(2024, 6, 1), dt.date(2024, 6, 20)]
    add("backtest.panel_from_frame", "two names, shuffled rows, a missing session, a state "
        "column: only SIGNAL is a signal; symbols from the first non-null",
        {"frame": pframe, "signal_column": "state", "column_of": probes})  # fmt: skip
    add("backtest.panel_from_frame", "a boolean signal column is read as is",
        {"frame": bool_frame, "signal_column": "hit", "column_of": probes})  # fmt: skip
    add("backtest.panel_from_frame", "no signal column, no symbol, no turnover: all-False and NaN",
        {"frame": pframe.drop("state", "symbol", "turnover_avg", "turnover_inr"),
         "signal_column": "state", "column_of": probes})  # fmt: skip
    add("backtest.panel_from_frame", "an empty frame: an empty panel",
        {"frame": pl.DataFrame(schema=pschema), "signal_column": "state", "column_of": probes})  # fmt: skip
    add("backtest.panel_from_frame", "over with_vbt_indicators + with_signal_columns output",
        {"frame": with_signal_columns(pipe, small)
            .select("instrument_id", "symbol", "date", "open", "high", "low", "close", "ema_exit",
                    "turnover_avg", "turnover_inr", "state")
            .filter(pl.col("date") >= d40[26]),
         "signal_column": "state", "column_of": [d40[30], d40[39]]})  # fmt: skip

    gframe = pl.DataFrame({"date": pdays[:4], "gate": ["OPEN", "SHUT", "OPEN", "OPEN"]},
                          schema={"date": pl.Date, "gate": pl.String})  # fmt: skip
    for sessions, frame, why in (
        (pdays, gframe, "aligned: sessions with no breadth row are SHUT"),
        (pdays[:4], gframe, "one row per session"),
        ([], gframe, "no sessions: an empty vector"),
        (pdays, pl.DataFrame(schema={"date": pl.Date, "gate": pl.String}), "no breadth at all: all SHUT"),
        ([pdays[2], pdays[0]], gframe, "the panel's order, not the frame's"),
    ):
        add("backtest.gate_vector", why, {"breadth": frame, "sessions": sessions})

    bt_cases = backtest_cases()
    results: dict[str, dict[str, Any]] = {}
    from baskfy_core.vbt.backtest import run_backtest

    for key, why, inputs in bt_cases:
        add("backtest.run_backtest", why, inputs)
        stored = json.loads(_golden().render(_golden()._enc(inputs)))
        result = run_backtest(panel_from(stored["panel"]), np.array(stored["gate_open"], dtype=bool),
                              params_from(stored["params"]))  # fmt: skip
        results[key] = {
            "params": stored["params"], "trades": result.trades, "sessions": result.sessions,
            "equity": result.equity, "open_positions": result.open_positions,
            "skipped": result.skipped, "orders_offered": result.orders_offered,
        }  # fmt: skip

    synth = synthetic_results()
    for key, why in (
        ("ema", "the planted EMA-exit trade"),
        ("caps", "a crowded session: caps, a loss and an end-of-run"),
        ("stops", "a stop gap and a same-session stop"),
        ("window", "a start/end window: warm-up columns are zero and skipped"),
    ):
        add("backtest.summarise", why, {"result": results[key]})
    add("backtest.summarise", "no sessions: None", {"result": synth["empty"]})
    add("backtest.summarise", "one non-zero point: None", {"result": synth["one"]})
    add("backtest.summarise", "a curve that only rises: no drawdown, calmar None; no losses, "
        "profit factor None", {"result": synth["rising"]})
    add("backtest.summarise", "a multi-year curve with wins, losses and a zero warm-up",
        {"result": synth["multi"]})
    add("backtest.summarise", "a flat curve: zero deviation, Sharpe NaN (null), no trades",
        {"result": synth["flat"]})

    add("backtest.yearly", "the planted trade, one year", {"result": results["ema"]})
    add("backtest.yearly", "three calendar years, trades closing in two",
        {"result": synth["multi"]})  # fmt: skip
    add("backtest.yearly", "no sessions: the live Python raises IndexError",
        {"result": synth["empty"]}, YEARLY_EMPTY_BUG)  # fmt: skip
    add("backtest.yearly", "a zero opening equity: return 0.0", {"result": synth["zero_open"]})
    add("backtest.yearly", "the crowded run", {"result": results["caps"]})

    add("backtest.BacktestResult.fill_rate_pct", "nothing offered: None", {"result": synth["empty"]})
    add("backtest.BacktestResult.fill_rate_pct", "one of one", {"result": results["ema"]})
    add("backtest.BacktestResult.fill_rate_pct", "orders offered that expired",
        {"result": results["expiry"]})  # fmt: skip
    add("backtest.BacktestResult.fill_rate_pct", "2 of 3, rounded to one decimal",
        {"result": {**synth["multi"], "orders_offered": 3}})  # fmt: skip
    return out


def backtest_cases() -> list[tuple[str, str, dict[str, Any]]]:  # noqa: PLR0915
    """``run_backtest`` scenarios over hand-built panels, quarter-rupee prices throughout."""
    days = weekdays(12, dt.date(2024, 6, 3))
    n = len(days)
    allopen = [True] * n
    out: list[tuple[str, str, dict[str, Any]]] = []

    def case(key: str, why: str, names: list[dict[str, Any]], gate: list[bool] | None = None,
             params: dict[str, Any] | None = None, sessions: list[dt.date] | None = None) -> None:  # fmt: skip
        out.append((key, why, {"panel": panel_json(sessions or days, names),
                               "gate_open": gate if gate is not None else allopen,
                               "params": params or params_json()}))  # fmt: skip

    # 1. The planted trade: signal at col 1, fill at col 2 on the pullback, EMA exit.
    x = flat_name(1, "XCO", n)
    x["signal"][1] = True
    set_bar(x, 2, open=101.0, high=102.0, low=99.5, close=101.0)
    set_bar(x, 5, close=97.0)
    set_bar(x, 6, open=96.5, high=97.0, low=96.0, close=96.5)
    case("ema", "the planted trade: limit at the signal close, filled on the pullback, sold at "
         "the next open after a close under the EMA", [x, flat_name(2, "BG", n, price=50.0)])

    # 2. Stops: a gap at the open, and a fresh entry stopped out on its own session.
    g = flat_name(1, "GAP", n)
    g["signal"][1] = True
    set_bar(g, 2, open=100.5, high=101.0, low=99.75, close=100.0)
    set_bar(g, 4, open=87.0, high=88.0, low=86.0, close=87.5)
    s = flat_name(2, "SAME", n, price=200.0, ema=190.0)
    s["signal"][3] = True
    set_bar(s, 4, open=200.5, high=201.0, low=170.0, close=172.0)
    h = flat_name(3, "HIT", n, price=40.0, ema=39.0)
    h["signal"][1] = True
    set_bar(h, 2, open=40.25, high=40.5, low=39.75, close=40.0)
    set_bar(h, 7, open=38.0, high=38.25, low=35.0, close=36.0)
    case("stops", "STOP_GAP at the open, a same-session STOP_HIT on the entry bar, and an "
         "intraday STOP_HIT at the stop", [g, s, h])

    # 3. No-bar write-off and end of run.
    w = flat_name(1, "GONE", n)
    w["signal"][0] = True
    set_bar(w, 1, low=99.0)
    blank(w, range(3, 10))
    e = flat_name(2, "HELD", n, price=60.0, ema=55.0)
    e["signal"][1] = True
    set_bar(e, 2, low=59.5)
    f = flat_name(3, "LAST", n, price=80.0, ema=70.0)
    f["signal"][2] = True
    set_bar(f, 3, low=79.0)
    blank(f, [n - 1])
    case("nobar", "a name that stops printing is written off at its last close after five blank "
         "sessions; positions still open at the end are liquidated (END_OF_RUN), at the entry when "
         "the last close is blank", [w, e, f])

    # 4. Expiry: a limit nobody trades down to.
    ex = flat_name(1, "AWAY", n)
    ex["signal"][1] = True
    for col in range(2, 6):
        set_bar(ex, col, open=104.0, high=105.0, low=103.0, close=104.0)
    late = flat_name(2, "LATE", n, price=50.0, ema=45.0)
    late["signal"][1] = True
    for col in (2, 3):
        set_bar(late, col, low=50.5)
    set_bar(late, 4, low=49.75)  # t+3: the last session it may fill on
    case("expiry", "a limit that is never touched expires after three sessions; one touched on "
         "t+3 still fills", [ex, late])

    # 5. Caps: five signals one session, three a session; two slots.
    crowd = []
    for k, sym in enumerate(("C1", "C2", "C3", "C4", "C5")):
        c = flat_name(k + 1, sym, n, price=100.0 + 10 * k, ema=90.0 + 10 * k, tinr=1e8 * (k + 1))
        c["signal"][1] = True
        set_bar(c, 2, low=99.0 + 10 * k)
        crowd.append(c)
    set_bar(crowd[4], 6, close=100.0)  # C5 (if held) falls under its EMA
    set_bar(crowd[4], 7, open=99.0, high=100.0, low=98.0, close=99.0)
    case("caps", "five signals on one session: ranked by turnover, three fill (session_cap), the "
         "rest keep working and fill the next session", crowd)
    case("slots", "two slots: slots_full", [dict(c, signal=list(c["signal"]), open=list(c["open"]),
         high=list(c["high"]), low=list(c["low"]), close=list(c["close"])) for c in crowd],
         params=params_json(with_config("sizing", max_slots=2)))  # fmt: skip

    # 6. The gate: shut at the signal's close, open a session later.
    gs = flat_name(1, "GATE", n)
    gs["signal"][1] = True
    for col in (2, 3):
        set_bar(gs, col, low=99.0)
    gate = [True] * n
    gate[1] = False
    case("gate", "the gate shut at the signal's close: no fill that session (gate_shut counts the "
         "signal), the order keeps working and fills when it reopens", [gs], gate=gate)
    case("gateshut", "the gate shut throughout: nothing trades, every signal counted", [dict(
        gs, signal=list(gs["signal"]))], gate=[False] * n)  # fmt: skip

    # 7. Locked and blank fill sessions.
    lk = flat_name(1, "LOCK", n)
    lk["signal"][1] = True
    set_bar(lk, 2, open=100.0, high=100.0, low=100.0, close=100.0)
    set_bar(lk, 3, low=99.0)
    nb = flat_name(2, "NOBAR", n, price=70.0, ema=60.0)
    nb["signal"][1] = True
    blank(nb, [2])
    set_bar(nb, 3, low=69.0)
    case("locked", "a locked bar (open = high = low) and a blank bar on the fill session: no "
         "fill, counted, and both fill on the next session", [lk, nb])

    # 8. Refusals: thin turnover and a tiny sleeve.
    thin = flat_name(1, "THIN", n, tavg=5e5)
    thin["signal"][1] = True
    set_bar(thin, 2, low=99.0)
    case("turnover", "1% of a Rs 5 lakh name is under the minimum trade: skipped as turnover", [thin])
    case("cash", "a Rs 5,000 sleeve cannot fund the minimum trade: skipped as cash",
         [dict(thin, tavg=[5e8] * n)], params=params_json(sleeve_inr="5000"))  # fmt: skip

    # 9. A window, a coarser tick, fill-through, and a NaN rank.
    wn = flat_name(1, "WIN", n, price=100.25, ema=99.0, tinr=math.nan)
    wn["signal"][3] = True
    set_bar(wn, 4, low=99.75)  # through 99.999375: would fill, but ranks last behind RANK
    set_bar(wn, 5, low=100.0)  # 100.00 is not through 100.25 x 0.9975 = 99.999375
    wr = flat_name(2, "RANK", n, price=30.25, ema=29.0)
    wr["signal"][3] = True
    set_bar(wr, 4, low=30.0)
    window_cfg = dataclasses.replace(
        with_config("entry", fill_through_pct=0.25),
        sizing=dataclasses.replace(default_config().sizing, max_new_entries_per_session=1),
    )
    case("window", "start/end restrict the walk; one entry a session and a NaN signal-day "
         "turnover ranking last, so RANK takes the session; fill_through 0.25% refuses WIN's "
         "100.00 low the next session and takes its 99.75 the one after; a 0.05 tick",
         [wn, wr], params=params_json(window_cfg, start=days[2], end=days[9], tick="0.05"))  # fmt: skip

    # 10. A held name signals again; a working name signals again.
    rs = flat_name(1, "AGAIN", n)
    rs["signal"][1] = True
    rs["signal"][3] = True  # held by then: ignored
    set_bar(rs, 2, low=99.0)
    rw = flat_name(2, "WAIT", n, price=50.0, ema=45.0)
    rw["signal"][1] = True
    rw["signal"][2] = True  # already working: ignored
    set_bar(rw, 4, low=49.5)
    case("resignal", "a fresh signal in a held name and in a working name is ignored", [rs, rw])

    # 11. Zero cost and a recalibrated stop.
    zc = flat_name(1, "ZERO", n)
    zc["signal"][1] = True
    set_bar(zc, 2, low=99.0)
    set_bar(zc, 5, open=95.0, high=95.5, low=89.0, close=91.0)
    case("cost", "no cost and a 10% stop: the stop at 90.00 is hit intraday",
         [zc], params=params_json(with_config("exits", stop_pct=10.0), cost_pct_per_side="0"))  # fmt: skip
    return out


def synthetic_results() -> dict[str, dict[str, Any]]:
    params = params_json()
    days3y = [dt.date(2023, 12, 27), dt.date(2023, 12, 28), dt.date(2023, 12, 29),
              dt.date(2024, 1, 1), dt.date(2024, 6, 3), dt.date(2024, 12, 31),
              dt.date(2025, 1, 1), dt.date(2025, 3, 3)]  # fmt: skip

    def trade(sym: str, entry: str, exit_: str, exit_date: dt.date, pnl: str, ret: str,  # noqa: PLR0913
              reason: str, r: str | None) -> dict[str, Any]:  # fmt: skip
        return {"symbol": sym, "instrument_id": 1, "entry_date": dt.date(2023, 12, 28),
                "exit_date": exit_date, "entry_price": entry, "exit_price": exit_,
                "quantity": 100, "pnl_inr": pnl, "return_pct": ret, "r_multiple": r,
                "hold_sessions": 3, "reason": reason, "cap": "SLOT"}  # fmt: skip

    def result(sessions: list[dt.date], equity: list[str], trades: list[dict[str, Any]],
               opens: list[int] | None = None, offered: int = 0) -> dict[str, Any]:  # fmt: skip
        return {"params": params, "trades": trades, "sessions": sessions, "equity": equity,
                "open_positions": opens if opens is not None else [1] * len(sessions),
                "skipped": {"expired": 0}, "orders_offered": offered}  # fmt: skip

    return {
        "empty": result([], [], []),
        "one": result(days3y[:2], ["0", "1000000"], []),
        "rising": result(days3y[:5], ["1000000", "1001000", "1003000", "1006000", "1010000"],
                         [trade("WIN", "100.25", "110.00", days3y[4], "975.00", "9.725", "EMA_EXIT",
                                "0.81")]),
        "multi": result(
            days3y,
            ["0", "1000000", "1012000.50", "990000.25", "1030000", "1100000", "1080000.75",
             "1120500"],
            [trade("A", "100", "112", days3y[4], "1200", "12", "EMA_EXIT", "1"),
             trade("B", "100", "88", days3y[5], "-1200", "-12", "STOP_HIT", "-1"),
             trade("C", "50", "50", days3y[7], "0", "0", "END_OF_RUN", "0")],
            opens=[0, 1, 2, 2, 1, 3, 2, 0], offered=4),
        "flat": result(days3y[:4], ["1000000"] * 4, [], opens=[0, 0, 0, 0]),
        "zero_open": result(days3y[:4], ["0", "0", "1000000", "1010000"], []),
    }


# =============================================================================================
# Dump
# =============================================================================================


def render_case(fn: str, why: str, inputs: dict[str, Any], known_bug: str | None = None) -> str:
    golden = _golden()
    # Round-trip the inputs through their JSON text first, so the stored output is the output of
    # exactly what the file says the inputs are.
    stored = json.loads(golden.render(golden._enc(inputs)))
    doc = golden.document(
        fn=fn, inputs=stored, output=CALLS[fn](stored), known_bug=known_bug,
        notes=f"{why}. {NOTES}", stable=True,
    )
    return golden.render(doc)


def dump_vbt() -> list[Path]:
    golden = _golden()
    directory = golden.GOLDEN_DIR / LANE
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    counters: dict[str, int] = {}
    for fn, why, inputs, known_bug in cases():
        stem = fn.removeprefix(PREFIX)
        counters[stem] = counters.get(stem, 0) + 1
        path = directory / f"{stem}.case_{counters[stem]:03d}.json"
        path.write_text(render_case(fn, why, inputs, known_bug), encoding="utf-8")
        written.append(path)
    return written


if __name__ == "__main__":
    paths = dump_vbt()
    print(f"{len(paths)} vbt goldens written under go/testdata/golden/{LANE}/")
