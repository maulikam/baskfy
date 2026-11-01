"""The TWT lane of the parity goldens: ``baskfy_core.twt``, the three-weeks-tight sleeve (Rust
rewrite wave 3, 29 Oct 2026).

    cd decile-blueprint
    PYTHONPATH=..:packages/core/src:packages/providers/src:packages/execution/src \\
        .venv/bin/python ../tools/parity/golden_twt.py

Dumps every public pure function of the sleeve's eleven modules into
``go/testdata/golden/L1/twt/<module>.<function>.case_NNN.json``, written ``stable=True`` by
``tools/parity/golden.py``'s own encoder, so two runs are byte-identical:

* ``calendar`` — ``session_counts``, ``thin_sessions``, ``build_calendar``, ``drop_thin_sessions``,
  ``as_shared_config``;
* ``indicators`` — ``bucket_keys`` (applied to a frame of dates), ``require_columns``,
  ``with_twt_indicators``;
* ``signals`` — ``weekly_column``, ``weekly_closes``, ``month_low_back``, ``tight_state``,
  ``entry_events``, ``liquidity_floor`` (applied with ``select``), ``signal_mask``,
  ``detect_signals``, ``with_twt_columns``;
* ``breadth`` — ``gate_for``, ``breadth_above_dma``, ``breadth_series``, ``as_shared_config``;
* ``sizing`` — ``size_entry``, ``first_live_multiplier``;
* ``exits`` — ``tick``, ``tick_floor``, ``initial_stop``, ``stop_is_below_entry``, ``trail_from``,
  ``clamp_under_close``, ``ratchet``, ``on_adjustment``, ``stop_fill``, ``fill_day_stop``,
  ``manage``, ``r_multiple``;
* ``sleeve`` — ``sleeve_value`` (with every derived property);
* ``plan`` — ``client_id_for``, ``stop_for``, ``build_entries``, ``exit_lines``, ``assemble``
  (with ``plan_hash``), ``gtt_limit_price``;
* ``drift`` — ``compare``; ``published`` — the ``PUBLISHED`` record;
* ``backtest`` — ``panel_from_frame``, ``gate_vector``, ``run_backtest``, ``summarise``,
  ``yearly``.

Conventions the reimplementation must not misread (restated in every case's ``meta.notes``):

* Every case's inputs are JSON, and every function is called from the stored inputs after a round
  trip through their JSON text — never from an object that was not written down.
* Decimals are strings; dates are ISO days; enums are their values. A config dataclass is its
  field dict, decoded field by field by the type of the field's **default** (so a ``Decimal``
  field read back from its string is a ``Decimal``, a float field from an int is a float) — the
  same rule as golden.py's ``_sub_config``.
* A polars frame is ``{"columns", "dtypes", "rows"}`` (golden.py's encoding). Two functions
  return a frame whose row order polars does not promise (a hash join after a group-by):
  ``weekly_closes`` and ``month_low_back``. Their outputs are sorted by ``(instrument_id, date)``
  before they are written, and the notes say so; every other frame is written in the order the
  function returned it.
* A function that raises stores ``{"raises": {"type", "message"}}``.
* A ``Panel``'s matrices are lists of rows (instruments) of columns (sessions); ``null`` is NaN.
* A ``float`` that is NaN or infinite is ``null`` (e.g. ``sharpe_ratio`` of a flat curve).
* ``drift.compare``'s ``cagr_pct``/``max_drawdown_pct`` are a JSON number (a float) or a string (a
  ``Decimal``); both are accepted by the function and both are exercised.
* Synthetic bars come from ``numpy.random.default_rng(seed)`` and are rounded to 2 dp before they
  are written, so the stored inputs, not the generator, are the contract.

Not dumped, and why: the private helpers (``_prepare``, ``_true``, ``_before_sizing``,
``_floor_div``, ``_dec``, ``_round_to_tick``, ``_bar_at``, ``_mark_equity``, ...) are reached
through the public functions; ``bucket_keys`` and ``liquidity_floor`` return polars expressions,
not data, so their goldens are the expressions applied to a stored frame; the dataclass
properties (``SizedEntry.placed``, ``SleeveValue.equity_inr``, ``TwtPlan.plan_hash``,
``BreadthReading.is_open``, ``Drift.to_json``, ``OpenPositionValue.value``...) are written into
the outputs of the functions that return them. Nothing in ``baskfy_core.twt`` is impure.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import importlib.util
import json
import sys
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CORE_SRC = ROOT / "decile-blueprint" / "packages" / "core" / "src"

try:  # the venv's editable install can point at a moved checkout; the source tree is the truth
    import baskfy_core  # noqa: F401
except ModuleNotFoundError:
    sys.path.insert(0, str(CORE_SRC))


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


LANE = "L1/twt"
NOTES = (
    "Inputs are JSON and each case is recomputed from them; Decimals are strings, dates ISO days, "
    "enums their values; a config is its field dict, decoded by each field's default type. Polars "
    "frames are {columns, dtypes, rows}; a Panel matrix is rows=instruments x cols=sessions with "
    "null for NaN; NaN/inf floats are null; a raise is {\"raises\": {\"type\", \"message\"}}. "
    "Prices: close is adjusted, close_raw is the exchange print (house rule 6). Regenerate with "
    "`PYTHONPATH=..:packages/core/src:packages/providers/src:packages/execution/src "
    ".venv/bin/python ../tools/parity/golden_twt.py` from decile-blueprint."
)
SORTED_NOTE = "Output rows sorted by (instrument_id, date): polars does not promise this order"

D = Decimal
START = dt.date(2024, 1, 1)


# ---------------------------------------------------------------------------
# Decoding helpers: JSON -> the objects each function takes
# ---------------------------------------------------------------------------


def _d(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _date(value: Any) -> dt.date | None:
    return None if value is None else dt.date.fromisoformat(value)


def _config(cls: Any, raw: dict[str, Any]) -> Any:
    """A config dataclass back from its JSON, each field by the type of its own default — the
    ``_sub_config`` rule, generalised to nested dataclasses, enums, dates and tuples."""
    default = cls()
    kwargs: dict[str, Any] = {}
    for key, value in raw.items():
        current = getattr(default, key)
        if value is None:
            kwargs[key] = None
        elif dataclasses.is_dataclass(current):
            kwargs[key] = _config(type(current), value)
        elif isinstance(current, enum.Enum):
            kwargs[key] = type(current)(value)
        elif isinstance(current, bool):
            kwargs[key] = bool(value)
        elif isinstance(current, Decimal):
            kwargs[key] = Decimal(str(value))
        elif isinstance(current, int):
            kwargs[key] = int(value)
        elif isinstance(current, float):
            kwargs[key] = float(value)
        elif isinstance(current, str):
            kwargs[key] = str(value)
        elif isinstance(current, dt.date):
            kwargs[key] = dt.date.fromisoformat(value)
        elif isinstance(current, tuple):
            kwargs[key] = tuple(value)
        else:
            raise TypeError(f"{cls.__name__}.{key}: no JSON rule for {type(current).__name__}")
    return cls(**kwargs)


def _twt_config(raw: dict[str, Any] | None) -> Any:
    from baskfy_core.twt.config import TwtConfig

    return TwtConfig() if raw is None else _config(TwtConfig, raw)


def _frame(doc: dict[str, Any]) -> Any:
    return _golden().polars_frame_from(doc)


def _calendar(doc: dict[str, Any]) -> Any:
    from baskfy_core.twt.calendar import SessionCalendar

    return SessionCalendar(
        sessions=tuple(_date(s) for s in doc["sessions"]),
        dropped=tuple(_date(s) for s in doc["dropped"]),
        counts={_date(k): int(v) for k, v in doc["counts"].items()},
    )


def _raised(exc: BaseException) -> dict[str, Any]:
    return {"raises": {"type": type(exc).__name__, "message": str(exc)}}


def _capture(call: Callable[[], Any]) -> Any:
    try:
        return call()
    except (ValueError, KeyError, TypeError) as exc:
        return _raised(exc)


def _sorted(frame: Any) -> Any:
    return frame.sort(["instrument_id", "date"])


# ---------------------------------------------------------------------------
# Fixture helpers: deterministic synthetic bars, written as polars docs
# ---------------------------------------------------------------------------

BAR_COLUMNS: tuple[str, ...] = (
    "instrument_id",
    "symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "close_raw",
    "volume",
    "adj_factor",
    "is_etf",
)
BAR_DTYPES: dict[str, str] = {
    "instrument_id": "Int64",
    "symbol": "String",
    "date": "Date",
    "open": "Float64",
    "high": "Float64",
    "low": "Float64",
    "close": "Float64",
    "close_raw": "Float64",
    "volume": "Float64",
    "adj_factor": "Float64",
    "is_etf": "Boolean",
}


def _weekdays(count: int, start: dt.date = START) -> list[dt.date]:
    out: list[dt.date] = []
    day = start
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += dt.timedelta(days=1)
    return out


def _doc(columns: list[str], dtypes: dict[str, str], rows: list[list[Any]]) -> dict[str, Any]:
    return {"columns": list(columns), "dtypes": {c: dtypes[c] for c in columns}, "rows": rows}


def _walk(seed: int, n: int, start: float = 100.0, vol: float = 0.02) -> list[float]:
    import numpy as np

    rng = np.random.default_rng(seed)
    steps = rng.normal(0.001, vol, n)
    out, price = [], start
    for step in steps:
        price *= 1 + float(step)
        out.append(round(price, 2))
    return out


def _series_rows(  # noqa: PLR0913 - a fixture is its knobs
    instrument_id: int,
    symbol: str | None,
    days: list[dt.date],
    closes: list[float | None],
    *,
    volume: float | list[float] = 100_000.0,
    spread: float = 0.01,
    close_raw: list[float | None] | None = None,
    is_etf: bool = False,
    locked: set[int] | None = None,
) -> list[list[Any]]:
    """One instrument's bars in BAR_COLUMNS order; a ``None`` close is a session it did not print."""
    rows: list[list[Any]] = []
    for i, (day, close) in enumerate(zip(days, closes, strict=True)):
        if close is None:
            continue
        if locked and i in locked:
            o = h = lo = close
        else:
            o = round(close * (1 - spread / 2), 2)
            h = round(close * (1 + spread), 2)
            lo = round(close * (1 - spread), 2)
        raw = close if close_raw is None else close_raw[i]
        vol = volume if isinstance(volume, float) else volume[i]
        rows.append([instrument_id, symbol, day, o, h, lo, close, raw, vol, 1.0, is_etf])
    return rows


def _bars(rows: list[list[Any]], columns: tuple[str, ...] = BAR_COLUMNS) -> dict[str, Any]:
    if columns != BAR_COLUMNS:
        index = [BAR_COLUMNS.index(c) for c in columns]
        rows = [[row[i] for i in index] for row in rows]
    return _doc(list(columns), BAR_DTYPES, rows)


def _calendar_doc(bars_doc: dict[str, Any]) -> dict[str, Any]:
    from baskfy_core.twt.calendar import build_calendar

    stored = json.loads(_golden().render(_golden()._enc(bars_doc)))
    return _golden()._enc(build_calendar(_frame(stored)))


def _cfg_doc(**sections: dict[str, Any]) -> dict[str, Any]:
    """A full TwtConfig doc, with per-section overrides."""
    from baskfy_core.twt.config import TwtConfig

    config = TwtConfig()
    for section, changes in sections.items():
        config = dataclasses.replace(
            config, **{section: dataclasses.replace(getattr(config, section), **changes)}
        )
    return _golden()._enc(config)


def _sub_doc(cls: Any, **changes: Any) -> dict[str, Any]:
    return _golden()._enc(dataclasses.replace(cls(), **changes))


# ---------------------------------------------------------------------------
# calendar
# ---------------------------------------------------------------------------


def call_session_counts(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.calendar import session_counts

    return _capture(lambda: session_counts(_frame(inputs["bars"])))


def call_thin_sessions(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.calendar import thin_sessions
    from baskfy_core.twt.config import DataConfig

    raw = inputs["config"]
    cfg = None if raw is None else _config(DataConfig, raw)
    return _capture(lambda: thin_sessions(_frame(inputs["bars"]), cfg))


def call_build_calendar(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.calendar import build_calendar

    return _capture(lambda: build_calendar(_frame(inputs["bars"]), _twt_config(inputs["config"])))


def call_drop_thin_sessions(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.calendar import drop_thin_sessions

    def run() -> Any:
        bars, calendar = drop_thin_sessions(_frame(inputs["bars"]), _twt_config(inputs["config"]))
        return {"bars": bars, "calendar": calendar}

    return _capture(run)


def call_calendar_shared(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.calendar import as_shared_config
    from baskfy_core.twt.config import DataConfig

    return as_shared_config(_config(DataConfig, inputs["config"]))


def _counts_bars(per_session: list[int], start: dt.date = START) -> dict[str, Any]:
    """Bars with only ``instrument_id`` and ``date``: ``per_session[i]`` names print on session i."""
    rows: list[list[Any]] = []
    for day, count in zip(_weekdays(len(per_session), start), per_session, strict=True):
        rows.extend([iid, day] for iid in range(1, count + 1))
    return _doc(["instrument_id", "date"], BAR_DTYPES, rows)


def _calendar_cases() -> list[tuple[str, str, dict[str, Any]]]:
    from baskfy_core.twt.config import DataConfig

    out: list[tuple[str, str, dict[str, Any]]] = []
    mod = "baskfy_core.twt.calendar."
    flat = [20] * 60
    thin = [20] * 60
    thin[30] = 3
    boundary_not = [20] * 60
    boundary_not[30] = 5  # 5 < 0.25 x 20 is false: not thin (strict)
    boundary_yes = [20] * 60
    boundary_yes[30] = 4
    two_thin = [20] * 60
    two_thin[10], two_thin[11] = 2, 2
    edge = [20] * 60
    edge[0], edge[59] = 3, 3  # the centred window still has min_periods at the ends
    short = [20, 20, 3, 20]  # fewer sessions than min_periods: no median, nothing thin
    custom = [20] * 60
    custom[25] = 8
    empty = _doc(["instrument_id", "date"], BAR_DTYPES, [])
    no_date = _doc(["instrument_id"], BAR_DTYPES, [[1], [2]])

    sc = mod + "session_counts"
    out.append((sc, "a full 3-name universe over 5 sessions", {"bars": _counts_bars([3] * 5)}))
    out.append((sc, "counts that vary by session", {"bars": _counts_bars([3, 1, 2, 5, 4])}))
    unsorted = _counts_bars([2, 3, 1])
    unsorted = {**unsorted, "rows": list(reversed(unsorted["rows"]))}
    out.append((sc, "rows in reverse order: the output is sorted by date", {"bars": unsorted}))
    out.append((sc, "an empty frame", {"bars": empty}))
    out.append((sc, "no date column: ValueError", {"bars": no_date}))

    ts = mod + "thin_sessions"
    for why, counts, cfg in (
        ("a flat universe: nothing thin", flat, None),
        ("one muhurat-like session of 3 names against 20", thin, None),
        ("5 names against a median of 20: not below 25% (strict)", boundary_not, None),
        ("4 names against a median of 20: thin", boundary_yes, None),
        ("two adjacent thin sessions", two_thin, None),
        ("thin sessions at both ends of the history (centred window)", edge, None),
        ("four sessions, under min_periods=5: no median, nothing thin", short, None),
        ("8 of 20 under a custom 0.5 share", custom, _sub_doc(DataConfig, thin_session_min_share=0.5)),
        ("8 of 20 under the default 0.25 share: not thin", custom, _sub_doc(DataConfig)),
    ):
        out.append((ts, why, {"bars": _counts_bars(counts), "config": cfg}))
    out.append((ts, "an empty frame", {"bars": empty, "config": None}))

    bc = mod + "build_calendar"
    for why, counts, cfg in (
        ("a flat universe: no session dropped", flat, _cfg_doc()),
        ("one thin session dropped and named", thin, _cfg_doc()),
        ("two adjacent thin sessions dropped", two_thin, _cfg_doc()),
        ("a custom 0.5 share drops the 8-name session", custom,
         _cfg_doc(data={"thin_session_min_share": 0.5})),
        ("a short history: nothing dropped", short, _cfg_doc()),
    ):  # fmt: skip
        out.append((bc, why, {"bars": _counts_bars(counts), "config": cfg}))
    out.append((bc, "an empty frame", {"bars": empty, "config": _cfg_doc()}))

    dts = mod + "drop_thin_sessions"
    small_thin = [4] * 12
    small_thin[6] = 1
    for why, counts, cfg in (
        ("nothing thin: the bars come back unchanged", [4] * 12, _cfg_doc()),
        ("the 1-name session's bar is removed", small_thin, _cfg_doc()),
        ("a window of 5 bars, min_periods 3", small_thin,
         _cfg_doc(data={"thin_session_window_bars": 5, "thin_session_min_periods": 3})),
        ("a share of 0.1 keeps the 1-of-4 session", small_thin,
         _cfg_doc(data={"thin_session_min_share": 0.1})),
    ):  # fmt: skip
        out.append((dts, why, {"bars": _counts_bars(counts), "config": cfg}))
    out.append((dts, "an empty frame", {"bars": empty, "config": _cfg_doc()}))

    shared = mod + "as_shared_config"
    out.append((shared, "the default DataConfig, spelled into VBT's shape",
                {"config": _sub_doc(DataConfig)}))  # fmt: skip
    out.append((shared, "custom thresholds travel field by field",
                {"config": _sub_doc(DataConfig, thin_session_min_share=0.3,
                                    thin_session_window_bars=21, thin_session_min_periods=4,
                                    rolling_min_share=0.8)}))  # fmt: skip
    return out


# ---------------------------------------------------------------------------
# indicators
# ---------------------------------------------------------------------------


def call_bucket_keys(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.indicators import bucket_keys

    frame = _frame(inputs["frame"])
    return frame.select("date", *bucket_keys())


def call_require_columns(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.indicators import require_columns

    return _capture(lambda: require_columns(_frame(inputs["bars"])))


def call_with_twt_indicators(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.indicators import with_twt_indicators

    return _capture(
        lambda: with_twt_indicators(
            _frame(inputs["bars"]), _calendar(inputs["calendar"]), _twt_config(inputs["config"])
        )
    )


def _indicator_cases() -> list[tuple[str, str, dict[str, Any]]]:
    out: list[tuple[str, str, dict[str, Any]]] = []
    mod = "baskfy_core.twt.indicators."

    bk = mod + "bucket_keys"
    boundary = [
        dt.date(2020, 12, 28), dt.date(2020, 12, 31), dt.date(2021, 1, 1), dt.date(2021, 1, 4),
        dt.date(2024, 12, 27), dt.date(2024, 12, 30), dt.date(2024, 12, 31), dt.date(2025, 1, 1),
        dt.date(2025, 1, 6), dt.date(2026, 12, 31), dt.date(2027, 1, 1), dt.date(2027, 1, 4),
    ]  # fmt: skip
    out.append((bk, "ISO-year week keys across three year boundaries (2020-W53, 2025-W01)",
                {"frame": _doc(["date"], BAR_DTYPES, [[d] for d in boundary])}))  # fmt: skip
    months = [dt.date(2024, m, 15) for m in range(1, 13)] + [dt.date(2025, 1, 2), dt.date(2025, 3, 3)]
    out.append((bk, "month keys year x 12 + month - 1, December to January",
                {"frame": _doc(["date"], BAR_DTYPES, [[d] for d in months])}))  # fmt: skip
    leap = [dt.date(2024, 2, 28), dt.date(2024, 2, 29), dt.date(2024, 3, 1), dt.date(2023, 1, 1),
            dt.date(2023, 1, 2), dt.date(2015, 12, 31), dt.date(2016, 1, 4)]  # fmt: skip
    out.append((bk, "a leap day, a Sunday that is ISO week 52 of the year before, 2015-W53",
                {"frame": _doc(["date"], BAR_DTYPES, [[d] for d in leap])}))  # fmt: skip
    out.append((bk, "an empty frame", {"frame": _doc(["date"], BAR_DTYPES, [])}))

    rc = mod + "require_columns"
    days = _weekdays(3)
    full = _bars(_series_rows(1, "A", days, [10.0, 11.0, 12.0]))
    out.append((rc, "every required column present: None", {"bars": full}))
    core = ("instrument_id", "date", "open", "high", "low", "close", "close_raw", "volume")
    out.append((rc, "only the required columns: None",
                {"bars": _bars(_series_rows(1, "A", days, [10.0, 11.0, 12.0]), core)}))  # fmt: skip
    out.append((rc, "close_raw missing: ValueError naming it",
                {"bars": _bars(_series_rows(1, "A", days, [10.0, 11.0, 12.0]),
                               tuple(c for c in BAR_COLUMNS if c != "close_raw"))}))  # fmt: skip
    out.append((rc, "open, volume and instrument_id missing: ValueError in REQUIRED order",
                {"bars": _bars(_series_rows(1, "A", days, [10.0, 11.0, 12.0]),
                               ("symbol", "date", "high", "low", "close", "close_raw"))}))  # fmt: skip
    out.append((rc, "an empty frame with every column: None", {"bars": _bars([])}))

    wi = mod + "with_twt_indicators"
    small = _cfg_doc(scan={"vol_sma_bars": 5}, entry={"turnover_avg_bars": 4},
                     breadth={"dma_bars": 10})  # fmt: skip
    d30 = _weekdays(30)
    a = _walk(11, 30)
    b = _walk(12, 30, start=40.0)
    two = _bars(_series_rows(1, "AAA", d30, a, volume=250_000.0)
                + _series_rows(2, "BBB", d30, b, volume=80_000.0))  # fmt: skip
    out.append((wi, "two names, 30 sessions, small windows (5/4/10)",
                {"bars": two, "calendar": _calendar_doc(two), "config": small}))  # fmt: skip
    out.append((wi, "the same bars under the default windows (50/20/200): averages warm up null",
                {"bars": two, "calendar": _calendar_doc(two), "config": _cfg_doc()}))  # fmt: skip
    holes = list(a)
    for i in (5, 6, 17):
        holes[i] = None
    holed = _bars(_series_rows(1, "AAA", d30, holes, volume=250_000.0)
                  + _series_rows(2, "BBB", d30, b, volume=80_000.0))  # fmt: skip
    out.append((wi, "three missing bars: densified to null rows, windows over sessions not rows "
                "(rolling_min_share 0.9 -> 5 of 5, 4 of 4, 9 of 10)",
                {"bars": holed, "calendar": _calendar_doc(holed), "config": small}))  # fmt: skip
    tolerant = _cfg_doc(scan={"vol_sma_bars": 5}, entry={"turnover_avg_bars": 4},
                        breadth={"dma_bars": 10}, data={"rolling_min_share": 0.6})  # fmt: skip
    out.append((wi, "the same holes under rolling_min_share 0.6 (3 of 5, 2 of 4, 6 of 10)",
                {"bars": holed, "calendar": _calendar_doc(holed), "config": tolerant}))  # fmt: skip
    d12 = _weekdays(12)
    locked = _bars(_series_rows(1, "LOCK", d12, _walk(13, 12), locked={4, 9}))
    out.append((wi, "limit_locked where open == high == low",
                {"bars": locked, "calendar": _calendar_doc(locked), "config": small}))  # fmt: skip
    bare = _bars(_series_rows(1, "A", d12, _walk(14, 12)), core)
    out.append((wi, "optional columns absent: adj_factor 1, is_etf false, symbol null",
                {"bars": bare, "calendar": _calendar_doc(bare), "config": small}))  # fmt: skip
    split_raw = [c * 2 if i < 6 else c for i, c in enumerate(_walk(15, 12))]
    split = _bars(_series_rows(1, "SPL", d12, _walk(15, 12), close_raw=split_raw,
                               volume=[50_000.0] * 6 + [100_000.0] * 6))  # fmt: skip
    out.append((wi, "turnover_inr reads close_raw (a 1:2 split before session 6)",
                {"bars": split, "calendar": _calendar_doc(split), "config": small}))  # fmt: skip
    cal = _calendar_doc(two)
    cal_short = {**cal, "sessions": cal["sessions"][:20], "dropped": cal["sessions"][20:]}
    out.append((wi, "a calendar shorter than the bars: rows off the calendar are dropped",
                {"bars": two, "calendar": cal_short, "config": small}))  # fmt: skip
    missing = _bars(_series_rows(1, "A", d12, _walk(14, 12)),
                    tuple(c for c in BAR_COLUMNS if c != "volume"))  # fmt: skip
    out.append((wi, "volume missing: ValueError before anything is computed",
                {"bars": missing, "calendar": _calendar_doc(bare), "config": small}))  # fmt: skip
    return out


# ---------------------------------------------------------------------------
# signals
# ---------------------------------------------------------------------------


def call_weekly_column(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.signals import weekly_column

    return weekly_column(int(inputs["back"]))


def call_weekly_closes(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.signals import weekly_closes

    return _sorted(weekly_closes(_frame(inputs["indicated"]), _twt_config(inputs["config"])))


def call_month_low_back(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.signals import month_low_back

    return _sorted(month_low_back(_frame(inputs["indicated"]), _twt_config(inputs["config"])))


def call_tight_state(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.signals import tight_state

    return tight_state(_frame(inputs["framed"]), _twt_config(inputs["config"]))


def call_entry_events(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.signals import entry_events

    return entry_events(_frame(inputs["stated"]), _twt_config(inputs["config"]))


def call_liquidity_floor(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.signals import liquidity_floor

    frame = _frame(inputs["frame"])
    expr = liquidity_floor(_twt_config(inputs["config"]), _d(inputs["floor_inr"]))
    return frame.select("turnover_avg_20", expr.alias("passes"))


def call_signal_mask(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.signals import signal_mask

    return signal_mask(
        _frame(inputs["detected"]), _twt_config(inputs["config"]), _d(inputs["floor_inr"])
    )


def call_detect_signals(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.signals import detect_signals

    return detect_signals(
        _frame(inputs["detected"]),
        _date(inputs["as_of"]),
        _twt_config(inputs["config"]),
        _d(inputs["floor_inr"]),
    )


def call_with_twt_columns(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.signals import with_twt_columns

    return with_twt_columns(
        _frame(inputs["bars"]), _calendar(inputs["calendar"]), _twt_config(inputs["config"])
    )


def _week_key(day: dt.date) -> int:
    iso = day.isocalendar()
    return iso.year * 100 + iso.week


def _month_key(day: dt.date) -> int:
    return day.year * 12 + day.month - 1


SIG_DTYPES: dict[str, str] = {
    **BAR_DTYPES,
    "week_key": "Int64",
    "month_key": "Int64",
    "weekly_close_0": "Float64",
    "weekly_close_1": "Float64",
    "weekly_close_2": "Float64",
    "month_low_back": "Float64",
    "vol_sma": "Float64",
    "tight_state": "Boolean",
    "entry_event": "Boolean",
    "turnover_inr": "Float64",
    "turnover_avg_20": "Float64",
    "signal_state": "String",
}


def _weekly_doc(series: list[tuple[int, list[dt.date], list[float | None]]]) -> dict[str, Any]:
    rows = []
    for iid, days, closes in series:
        for day, close in zip(days, closes, strict=True):
            rows.append([iid, day, _week_key(day), close])
    return _doc(["instrument_id", "date", "week_key", "close"], SIG_DTYPES, rows)


def _monthly_doc(series: list[tuple[int, list[dt.date], list[float | None]]]) -> dict[str, Any]:
    rows = []
    for iid, days, lows in series:
        for day, low in zip(days, lows, strict=True):
            rows.append([iid, day, _month_key(day), low])
    return _doc(["instrument_id", "date", "month_key", "low"], SIG_DTYPES, rows)


def _tight_bars(  # noqa: PLR0913 - the shape of `04` §3, as knobs
    *,
    count: int = 100,
    start: dt.date = START,
    base: float = 60.0,
    close: float = 90.0,
    w1: float | None = None,
    w2: float | None = None,
    close_raw: float | None = None,
    volume: float = 300_000.0,
    iid: int = 1,
    symbol: str = "TWTCO",
    is_etf: bool = False,
) -> list[list[Any]]:
    """Flat at ``base``; today's close and the last close of each of the two preceding ISO weeks
    placed on their own sessions (twt_fixtures.tight_bars, reimplemented)."""
    days = _weekdays(count, start)
    closes: list[float | None] = [base] * count
    raws: list[float | None] = [base] * count
    closes[-1] = close
    raws[-1] = close if close_raw is None else close_raw
    keys = sorted({_week_key(d) for d in days})
    here = keys.index(_week_key(days[-1]))
    for back, value in ((1, w1 if w1 is not None else close), (2, w2 if w2 is not None else close)):
        target = keys[here - back]
        last = max(i for i, d in enumerate(days) if _week_key(d) == target)
        closes[last] = value
        raws[last] = value
    rows = _series_rows(iid, symbol, days, closes, volume=volume, spread=0.0,
                        close_raw=raws, is_etf=is_etf)  # fmt: skip
    return rows


def _signal_cases() -> list[tuple[str, str, dict[str, Any]]]:  # noqa: PLR0915 - a flat table
    out: list[tuple[str, str, dict[str, Any]]] = []
    mod = "baskfy_core.twt.signals."
    cfg = _cfg_doc()

    wc = mod + "weekly_column"
    for back in (0, 1, 2, 10):
        out.append((wc, f"weekly_column({back})", {"back": back}))

    wk = mod + "weekly_closes"
    d15 = _weekdays(15)  # three ISO weeks
    out.append((wk, "one name over three full weeks",
                {"indicated": _weekly_doc([(1, d15, [float(100 + i) for i in range(15)])]),
                 "config": cfg}))  # fmt: skip
    yb = _weekdays(15, dt.date(2024, 12, 16))
    out.append((wk, "across 2024-W52 -> 2025-W01: ISO-year keys sort in time order",
                {"indicated": _weekly_doc([(1, yb, [float(200 + i) for i in range(15)])]),
                 "config": cfg}))  # fmt: skip
    gap_week: list[float | None] = [float(50 + i) for i in range(15)]
    for i in range(5, 10):
        gap_week[i] = None
    out.append((wk, "a name with no close in the middle week: w1/w2 null where they read it, "
                "never an earlier week",
                {"indicated": _weekly_doc([(1, d15, gap_week), (2, d15, [70.0] * 15)]),
                 "config": cfg}))  # fmt: skip
    late_null: list[float | None] = [float(10 + i) for i in range(15)]
    late_null[4] = None  # Friday of week 1 missing: last non-null is Thursday
    out.append((wk, "a null Friday close: the week's last non-null close is used",
                {"indicated": _weekly_doc([(1, d15, late_null)]), "config": cfg}))  # fmt: skip
    holiday = [d for d in _weekdays(20) if not (dt.date(2024, 1, 8) <= d <= dt.date(2024, 1, 12))]
    out.append((wk, "an ISO week with no session in the data: buckets are positions in the data",
                {"indicated": _weekly_doc([(1, holiday, [float(30 + i) for i in range(15)])]),
                 "config": cfg}))  # fmt: skip
    out.append((wk, "tight_weeks=4 adds weekly_close_3",
                {"indicated": _weekly_doc([(1, _weekdays(20), [float(100 + i) for i in range(20)])]),
                 "config": _cfg_doc(scan={"tight_weeks": 4})}))  # fmt: skip
    out.append((wk, "a single week: w1 and w2 null",
                {"indicated": _weekly_doc([(1, _weekdays(5), [1.0, 2.0, 3.0, 4.0, 5.0])]),
                 "config": cfg}))  # fmt: skip
    out.append((wk, "two names on different histories (one listed in week 2)",
                {"indicated": _weekly_doc([(1, d15, [float(100 + i) for i in range(15)]),
                                           (2, d15[5:], [float(300 - i) for i in range(10)])]),
                 "config": cfg}))  # fmt: skip

    ml = mod + "month_low_back"
    d130 = _weekdays(130)  # Jan..Jun 2024
    lows = [float(100 - (i % 23)) for i in range(130)]
    out.append((ml, "one name, Jan-Jun: April reads January's low, June reads March's",
                {"indicated": _monthly_doc([(1, d130, lows)]), "config": cfg}))  # fmt: skip
    yb = _weekdays(110, dt.date(2024, 10, 1))
    out.append((ml, "across the year: January 2025 reads October 2024, March reads December",
                {"indicated": _monthly_doc([(1, yb, [float(50 + (i % 17)) for i in range(110)])]),
                 "config": cfg}))  # fmt: skip
    absent = [None if 20 <= i < 45 else float(80 + (i % 7)) for i in range(130)]
    out.append((ml, "a name that did not print in its month-3: null, as is a null low",
                {"indicated": _monthly_doc([(1, d130, absent), (2, d130, lows)]),
                 "config": cfg}))  # fmt: skip
    out.append((ml, "month_low_months_back=2",
                {"indicated": _monthly_doc([(1, d130, lows)]),
                 "config": _cfg_doc(scan={"month_low_months_back": 2})}))  # fmt: skip
    out.append((ml, "a short history: no session has a month-3",
                {"indicated": _monthly_doc([(1, _weekdays(40), [float(i + 1) for i in range(40)])]),
                 "config": cfg}))  # fmt: skip

    ts = mod + "tight_state"
    tcols = ["instrument_id", "date", "close", "close_raw", "vol_sma", "month_low_back",
             "weekly_close_0", "weekly_close_1", "weekly_close_2", "is_etf"]  # fmt: skip

    def trow(i: int, close: float | None, raw: float | None, vol: float | None,
             mlow: float | None, w: tuple[float | None, float | None, float | None],
             etf: bool | None = False) -> list[Any]:  # fmt: skip
        return [i, START + dt.timedelta(days=i), close, raw, vol, mlow, *w, etf]

    passing = trow(1, 90.0, 90.0, 20_000.0, 60.0, (90.0, 89.0, 88.0))
    out.append((ts, "line by line: a pass, then each line failing alone",
                {"framed": _doc(tcols, SIG_DTYPES, [
                    passing,
                    trow(2, 90.0, 29.0, 20_000.0, 60.0, (90.0, 89.0, 88.0)),   # line 1
                    trow(3, 70.0, 70.0, 20_000.0, 60.0, (70.0, 69.5, 69.0)),   # line 2
                    trow(4, 90.0, 90.0, 20_000.0, 60.0, (90.0, 85.0, 88.0)),   # line 3
                    trow(5, 90.0, 90.0, 9_999.0, 60.0, (90.0, 89.0, 88.0)),    # line 5
                    trow(6, 90.0, 90.0, 20_000.0, 60.0, (90.0, 89.0, 88.0), True),  # ETF
                ]), "config": cfg}))  # fmt: skip
    out.append((ts, "boundaries: close_raw exactly 30 fails (strict); close exactly 1.3 x low "
                "passes; vol_sma exactly 10000 passes; range exactly 3% passes",
                {"framed": _doc(tcols, SIG_DTYPES, [
                    trow(1, 90.0, 30.0, 20_000.0, 60.0, (90.0, 89.0, 88.0)),
                    trow(2, 78.0, 78.0, 20_000.0, 60.0, (78.0, 78.0, 78.0)),
                    trow(3, 90.0, 90.0, 10_000.0, 60.0, (90.0, 89.0, 88.0)),
                    trow(4, 103.0, 103.0, 20_000.0, 60.0, (103.0, 100.0, 101.0)),
                    trow(5, 103.02, 103.02, 20_000.0, 60.0, (103.02, 100.0, 101.0)),
                ]), "config": cfg}))  # fmt: skip
    out.append((ts, "a null anywhere is a fail, never a pass",
                {"framed": _doc(tcols, SIG_DTYPES, [
                    trow(1, 90.0, 90.0, 20_000.0, 60.0, (90.0, None, 88.0)),
                    trow(2, 90.0, 90.0, 20_000.0, None, (90.0, 89.0, 88.0)),
                    trow(3, 90.0, 90.0, None, 60.0, (90.0, 89.0, 88.0)),
                    trow(4, 90.0, None, 20_000.0, 60.0, (90.0, 89.0, 88.0)),
                    trow(5, None, None, None, None, (None, None, None)),
                ]), "config": cfg}))  # fmt: skip
    out.append((ts, "a 5% band and a 1.5 multiple from the config",
                {"framed": _doc(tcols, SIG_DTYPES, [
                    trow(1, 90.0, 90.0, 20_000.0, 60.0, (90.0, 86.0, 88.0)),
                    trow(2, 89.0, 89.0, 20_000.0, 60.0, (89.0, 88.0, 88.5)),
                    trow(3, 90.0, 90.0, 20_000.0, 60.0, (90.0, 89.0, 88.0)),
                ]), "config": _cfg_doc(scan={"tight_band_pct": D("5"),
                                             "month_low_multiple": D("1.5")})}))  # fmt: skip
    out.append((ts, "an empty frame", {"framed": _doc(tcols, SIG_DTYPES, []), "config": cfg}))

    ee = mod + "entry_events"
    ecols = ["instrument_id", "date", "close", "tight_state"]
    d14 = _weekdays(14)

    def stated(series: list[tuple[int, list[bool | None], list[float | None] | None]]) -> dict[str, Any]:  # noqa: E501
        rows = []
        for iid, states, closes in series:
            for i, state in enumerate(states):
                close = 100.0 if closes is None else closes[i]
                rows.append([iid, d14[i], close, state])
        return _doc(ecols, SIG_DTYPES, rows)

    t, f = True, False
    out.append((ee, "five sessions out, then the state: an event on session 6+ of history",
                {"stated": stated([(1, [f] * 8 + [t, t, f, f, f, f], None)]),
                 "config": cfg}))  # fmt: skip
    out.append((ee, "in, out for 4, in again: no event (needs 5 out); out for 5: event",
                {"stated": stated([(1, [f] * 6 + [t, f, f, f, f, t, f, f], None),
                                   (2, [f] * 6 + [t, f, f, f, f, f, t, f], None)]),
                 "config": cfg}))  # fmt: skip
    out.append((ee, "a fresh listing in the state on its first sessions: no event until 5 "
                "sessions of history precede it (TW0.6)",
                {"stated": stated([(1, [t] * 14, None),
                                   (2, [f, f, t, t, t, t, t, t, t, t, t, t, t, t],
                                    [None, None] + [50.0] * 12)]),
                 "config": cfg}))  # fmt: skip
    out.append((ee, "never in the state: sessions_out_before null throughout",
                {"stated": stated([(1, [f] * 14, None)]), "config": cfg}))  # fmt: skip
    rev = stated([(2, [f] * 7 + [t] + [f] * 6, None), (1, [f] * 9 + [t] + [f] * 4, None)])
    rev = {**rev, "rows": list(reversed(rev["rows"]))}
    out.append((ee, "two names, rows in reverse order: sorted by (instrument_id, date)",
                {"stated": rev, "config": cfg}))  # fmt: skip
    out.append((ee, "entry_min_sessions_out=2",
                {"stated": stated([(1, [f, f, t, f, f, t, f, t, f, f, f, t, t, f], None)]),
                 "config": _cfg_doc(entry={"entry_min_sessions_out": 2})}))  # fmt: skip
    out.append((ee, "a null state is read as out of the state",
                {"stated": stated([(1, [f, None, f, None, f, f, t, f, f, f, f, f, f, f], None)]),
                 "config": cfg}))  # fmt: skip

    lf = mod + "liquidity_floor"
    lrows = [[v] for v in (None, 0.0, 19_999_999.99, 20_000_000.0, 20_000_000.01, 5e7)]
    ldoc = _doc(["turnover_avg_20"], SIG_DTYPES, lrows)
    out.append((lf, "the shipped floor of 2 crore: >= passes, null fails",
                {"frame": ldoc, "config": cfg, "floor_inr": None}))  # fmt: skip
    out.append((lf, "floor_inr overrides the config (5 crore)",
                {"frame": ldoc, "config": cfg, "floor_inr": D("50000000")}))  # fmt: skip
    out.append((lf, "a config floor of 1 crore",
                {"frame": ldoc, "config": _cfg_doc(entry={"min_turnover_inr": D("10000000")}),
                 "floor_inr": None}))  # fmt: skip

    sm = mod + "signal_mask"
    mcols = ["entry_event", "turnover_avg_20"]
    mrows = [[True, 3e7], [True, 1e7], [False, 3e7], [None, 3e7], [True, None], [True, 2e7]]
    mdoc = _doc(mcols, SIG_DTYPES, mrows)
    out.append((sm, "event AND floor; a null on either side is false",
                {"detected": mdoc, "config": cfg, "floor_inr": None}))  # fmt: skip
    out.append((sm, "floor_inr 1 crore lets the 1-crore row through",
                {"detected": mdoc, "config": cfg, "floor_inr": D("10000000")}))  # fmt: skip
    out.append((sm, "an empty frame", {"detected": _doc(mcols, SIG_DTYPES, []), "config": cfg,
                                        "floor_inr": None}))  # fmt: skip

    ds = mod + "detect_signals"
    dcols = ["instrument_id", "symbol", "date", "entry_event", "turnover_inr", "turnover_avg_20"]
    s1, s2 = dt.date(2024, 5, 20), dt.date(2024, 5, 21)
    drows = [
        [1, "AAA", s1, True, 4e7, 3e7],
        [2, "BBB", s1, True, 9e7, 1e7],   # below the floor: SCAN_ONLY
        [3, "CCC", s1, False, 9e9, 9e9],  # not an event: dropped
        [4, "DDD", s1, True, 4e7, 2.5e7],  # ties AAA on rank_key: symbol ascending
        [5, "EEE", s2, True, 1e7, 5e7],
        [6, "FFF", s2, True, None, 5e7],  # null rank sorts last
        [7, "GGG", s2, None, 5e7, 5e7],
    ]  # fmt: skip
    ddoc = _doc(dcols, SIG_DTYPES, drows)
    out.append((ds, "the whole history (as_of None): ranked by date, rank_key desc, symbol",
                {"detected": ddoc, "as_of": None, "config": cfg, "floor_inr": None}))  # fmt: skip
    out.append((ds, "as_of restricts to one session",
                {"detected": ddoc, "as_of": s1, "config": cfg, "floor_inr": None}))  # fmt: skip
    out.append((ds, "an as_of with no rows: the empty frame with the added columns",
                {"detected": ddoc, "as_of": dt.date(2024, 5, 22), "config": cfg,
                 "floor_inr": None}))  # fmt: skip
    out.append((ds, "floor_inr 1 crore promotes BBB to SIGNAL",
                {"detected": ddoc, "as_of": s1, "config": cfg, "floor_inr": D("10000000")}))
    out.append((ds, "a config floor of 4 crore",
                {"detected": ddoc, "as_of": None,
                 "config": _cfg_doc(entry={"min_turnover_inr": D("40000000")}),
                 "floor_inr": None}))  # fmt: skip

    wtc = mod + "with_twt_columns"

    def e2e(why: str, rows: list[list[Any]], config: dict[str, Any] | None = None) -> None:
        doc = _bars(rows)
        out.append((wtc, why, {"bars": doc, "calendar": _calendar_doc(doc),
                               "config": config or cfg}))  # fmt: skip

    e2e("tight at 90 over a month-3 low of 60: the state and an entry event on the last session",
        _tight_bars())
    e2e("w1 3.5% below today: not tight", _tight_bars(w1=86.95))
    e2e("close_raw 28 (a split in the adjusted series): line 1 fails", _tight_bars(close_raw=28.0))
    e2e("an ETF: never in the state", _tight_bars(is_etf=True))
    e2e("volume 9,000: line 5 fails", _tight_bars(volume=9_000.0))
    e2e("close 77 under 1.3 x 60: line 2 fails", _tight_bars(close=77.0))
    e2e("a history ending in 2025-W01 (year boundary)",
        _tight_bars(start=dt.date(2024, 8, 19)))
    e2e("two names on one calendar, one tight and one not",
        _tight_bars() + _tight_bars(iid=2, symbol="LOOSE", w2=80.0))
    return out


# ---------------------------------------------------------------------------
# breadth
# ---------------------------------------------------------------------------


def call_gate_for(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.breadth import gate_for
    from baskfy_core.twt.config import BreadthConfig

    raw = inputs["config"]
    return gate_for(D(inputs["pct_above_dma"]), None if raw is None else _config(BreadthConfig, raw))


def call_breadth_above_dma(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.breadth import breadth_above_dma

    reading = breadth_above_dma(
        _frame(inputs["indicated"]), _date(inputs["as_of"]), _twt_config(inputs["config"])
    )
    return {"reading": reading, "derived": {"is_open": reading.is_open}}


def call_breadth_series(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.breadth import breadth_series

    return breadth_series(_frame(inputs["indicated"]), _twt_config(inputs["config"]))


def call_breadth_shared(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.breadth import as_shared_config
    from baskfy_core.twt.config import BreadthConfig

    return as_shared_config(_config(BreadthConfig, inputs["config"]))


def _breadth_cases() -> list[tuple[str, str, dict[str, Any]]]:
    from baskfy_core.twt.config import BreadthConfig

    out: list[tuple[str, str, dict[str, Any]]] = []
    mod = "baskfy_core.twt.breadth."
    cfg = _cfg_doc()

    gf = mod + "gate_for"
    for pct, raw, why in (
        ("40.0001", None, "a hair above 40: OPEN"),
        ("40.0000", None, "exactly 40: SHUT (strict)"),
        ("39.9999", None, "below: SHUT"),
        ("0.0000", None, "an unmeasurable session: SHUT"),
        ("100.0000", None, "everything above: OPEN"),
        ("36.0000", _sub_doc(BreadthConfig, min_pct_above_dma=35.0), "36 over a 35 threshold"),
        ("35.0000", _sub_doc(BreadthConfig, min_pct_above_dma=35.0), "35 at a 35 threshold"),
    ):
        out.append((gf, why, {"pct_above_dma": pct, "config": raw}))

    bcols = ["instrument_id", "date", "close", "sma_dma"]
    s1, s2, s3 = dt.date(2024, 6, 3), dt.date(2024, 6, 4), dt.date(2024, 6, 5)

    def panel(rows: list[tuple[int, dt.date, float | None, float | None]]) -> dict[str, Any]:
        return _doc(bcols, {**BAR_DTYPES, "sma_dma": "Float64"}, [list(r) for r in rows])

    mixed = panel([
        (1, s1, 110.0, 100.0), (2, s1, 90.0, 100.0), (3, s1, 100.0, 100.0),  # equal: not above
        (4, s1, 50.0, None),  # warming up: in the universe, not measured
        (5, s1, None, 40.0),  # no bar: in neither
        (1, s2, 120.0, 100.0), (2, s2, 120.0, 100.0), (3, s2, 99.0, 100.0),
        (1, s3, 101.0, 100.0), (2, s3, 99.0, 100.0), (3, s3, 98.0, 100.0), (4, s3, 97.0, 100.0),
        (5, s3, 105.0, 100.0),
    ])  # fmt: skip
    bad = mod + "breadth_above_dma"
    out.append((bad, "as_of s1: 1 above of 3 measured (equal is not above), 4 with a bar",
                {"indicated": mixed, "as_of": s1, "config": cfg}))  # fmt: skip
    out.append((bad, "as_of s2: 2 of 3, 66.6667 to four places",
                {"indicated": mixed, "as_of": s2, "config": cfg}))  # fmt: skip
    out.append((bad, "as_of None counts every row of the frame and labels it with the latest session",
                {"indicated": mixed, "as_of": None, "config": cfg}))  # fmt: skip
    out.append((bad, "a session nothing can measure: 0.0000 and SHUT",
                {"indicated": panel([(1, s1, 10.0, None), (2, s1, 11.0, None)]), "as_of": s1,
                 "config": cfg}))  # fmt: skip
    out.append((bad, "an empty frame with as_of None: session null",
                {"indicated": panel([]), "as_of": None, "config": cfg}))  # fmt: skip
    out.append((bad, "an as_of the frame does not hold: zero counts, the asked session",
                {"indicated": mixed, "as_of": dt.date(2024, 6, 10), "config": cfg}))  # fmt: skip
    out.append((bad, "s1 under a 30% threshold: 33.3333 is OPEN",
                {"indicated": mixed, "as_of": s1,
                 "config": _cfg_doc(breadth={"min_pct_above_dma": 30.0})}))  # fmt: skip

    bs = mod + "breadth_series"
    out.append((bs, "three sessions, one row each, rounded before the gate reads it",
                {"indicated": mixed, "config": cfg}))  # fmt: skip
    out.append((bs, "pct_decimals 2 and a 33.33 threshold: 33.3333 rounds to 33.33, SHUT",
                {"indicated": mixed,
                 "config": _cfg_doc(breadth={"pct_decimals": 2, "min_pct_above_dma": 33.33})}))
    out.append((bs, "nothing measured on any session: 0.0 and SHUT",
                {"indicated": panel([(1, s1, 10.0, None), (1, s2, None, None)]),
                 "config": cfg}))  # fmt: skip
    out.append((bs, "an empty frame", {"indicated": panel([]), "config": cfg}))
    out.append((bs, "rows in reverse order: sorted by date",
                {"indicated": {**mixed, "rows": list(reversed(mixed["rows"]))},
                 "config": cfg}))  # fmt: skip

    sh = mod + "as_shared_config"
    out.append((sh, "the default BreadthConfig in VBT's shape", {"config": _sub_doc(BreadthConfig)}))
    out.append((sh, "a custom 150-bar, 35% gate travels field by field",
                {"config": _sub_doc(BreadthConfig, dma_bars=150, min_pct_above_dma=35.0)}))
    return out


# ---------------------------------------------------------------------------
# sizing
# ---------------------------------------------------------------------------


def call_size_entry(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.config import SizingConfig
    from baskfy_core.twt.sizing import size_entry

    sized = size_entry(
        equity=D(inputs["equity"]),
        cash_available=D(inputs["cash_available"]),
        entry_price=D(inputs["entry_price"]),
        stop_price=D(inputs["stop_price"]),
        turnover_avg_inr=_d(inputs["turnover_avg_inr"]),
        config=_config(SizingConfig, inputs["config"]),
        slot_multiplier=D(inputs["slot_multiplier"]),
    )
    return {
        "sized": sized,
        "derived": {"placed": sized.placed, "turnover_capped": sized.turnover_capped},
    }


def call_first_live_multiplier(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.config import SizingConfig
    from baskfy_core.twt.sizing import first_live_multiplier

    return first_live_multiplier(
        entries_left=int(inputs["entries_left"]),
        execution_enabled=bool(inputs["execution_enabled"]),
        config=_config(SizingConfig, inputs["config"]),
    )


def _sizing_cases() -> list[tuple[str, str, dict[str, Any]]]:
    from baskfy_core.twt.config import SizingConfig

    out: list[tuple[str, str, dict[str, Any]]] = []
    se = "baskfy_core.twt.sizing.size_entry"
    base = {
        "equity": D("2500000"),
        "cash_available": D("2500000"),
        "entry_price": D("412.35"),
        "stop_price": D("329.85"),
        "turnover_avg_inr": D("500000000"),
        "config": _sub_doc(SizingConfig),
        "slot_multiplier": D("1"),
    }
    for why, over in (
        ("the slot: a tenth of 25 lakh", {}),
        ("five slots: the slot (20%) is capped at 12.5% of equity",
         {"config": _sub_doc(SizingConfig, max_slots=5)}),
        ("a name at the 2 crore floor: 1% of turnover (2 lakh) caps the slot",
         {"turnover_avg_inr": D("20000000")}),
        ("only 1.5 lakh of cash: CASH", {"cash_available": D("150000")}),
        ("five slots, a thin name and little cash: every cap applies, in order",
         {"config": _sub_doc(SizingConfig, max_slots=5), "turnover_avg_inr": D("25000000"),
          "cash_available": D("120000")}),
        ("no turnover reading: the turnover cap is not applied",
         {"turnover_avg_inr": None, "config": _sub_doc(SizingConfig, max_slots=5)}),
        ("equity zero: NO_SLEEVE_CAPITAL", {"equity": D("0")}),
        ("an entry of zero: STOP_NOT_BELOW_ENTRY", {"entry_price": D("0")}),
        ("stop equal to entry: STOP_NOT_BELOW_ENTRY", {"stop_price": D("412.35")}),
        ("a zero stop: STOP_NOT_BELOW_ENTRY", {"stop_price": D("0")}),
        ("8,000 of cash: BELOW_MIN_TRADE_VALUE", {"cash_available": D("8000")}),
        ("negative cash is read as zero: BELOW_MIN_TRADE_VALUE", {"cash_available": D("-5000")}),
        ("half a slot (first live)", {"slot_multiplier": D("0.5")}),
        ("a price above the slot: zero shares, BELOW_MIN_TRADE_VALUE",
         {"entry_price": D("300000"), "stop_price": D("240000")}),
        ("an inexact slot: 3 slots of 1,000,000 against 333.33",
         {"equity": D("1000000"), "cash_available": D("1000000"),
          "config": _sub_doc(SizingConfig, max_slots=3, max_position_pct=D("50")),
          "entry_price": D("333.33"), "stop_price": D("266.65")}),
    ):  # fmt: skip
        out.append((se, why, {**base, **over}))

    fl = "baskfy_core.twt.sizing.first_live_multiplier"
    for left, enabled, why in (
        (10, True, "live, ten entries left: half"),
        (1, True, "live, the last half-size entry"),
        (0, True, "live, all ten taken: full"),
        (5, False, "DRY_RUN: always full"),
        (-1, True, "a negative counter: full"),
    ):
        out.append((fl, why, {"entries_left": left, "execution_enabled": enabled,
                              "config": _sub_doc(SizingConfig)}))  # fmt: skip
    out.append((fl, "a custom 0.25 first-live multiplier",
                {"entries_left": 3, "execution_enabled": True,
                 "config": _sub_doc(SizingConfig, risk_multiplier_first_live=D("0.25"))}))
    return out


# ---------------------------------------------------------------------------
# exits
# ---------------------------------------------------------------------------


def _position(raw: dict[str, Any]) -> Any:
    from baskfy_core.twt.exits import OpenPosition

    return OpenPosition(
        instrument_id=int(raw["instrument_id"]),
        entry_date=_date(raw["entry_date"]),
        fill_price=D(raw["fill_price"]),
        quantity=int(raw["quantity"]),
        stop_price=D(raw["stop_price"]),
        initial_stop=D(raw["initial_stop"]),
        high_since=D(raw["high_since"]),
    )


def _bar(raw: dict[str, Any]) -> Any:
    from baskfy_core.twt.exits import Bar

    return Bar(
        session=_date(raw["session"]),
        open=_d(raw["open"]),
        high=_d(raw["high"]),
        low=_d(raw["low"]),
        close=_d(raw["close"]),
    )


def _exit_cfg(raw: dict[str, Any]) -> Any:
    from baskfy_core.twt.config import ExitConfig

    return _config(ExitConfig, raw)


def call_tick(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.exits import tick

    return tick(inputs["tick_inr"])


def call_tick_floor(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.exits import tick_floor

    return tick_floor(D(inputs["price"]), D(inputs["tick_size"]))


def call_initial_stop(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.exits import initial_stop

    return initial_stop(D(inputs["fill_price"]), _exit_cfg(inputs["config"]), D(inputs["tick_size"]))


def call_stop_is_below_entry(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.exits import stop_is_below_entry

    return stop_is_below_entry(D(inputs["fill_price"]), D(inputs["stop"]))


def call_trail_from(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.exits import trail_from

    return trail_from(D(inputs["high_since"]), _exit_cfg(inputs["config"]))


def call_clamp_under_close(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.exits import clamp_under_close

    return clamp_under_close(
        D(inputs["raw_trigger"]), D(inputs["close"]), _exit_cfg(inputs["config"]),
        D(inputs["tick_size"]),
    )  # fmt: skip


def call_ratchet(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.exits import ratchet

    return ratchet(
        _position(inputs["position"]), _bar(inputs["bar"]), _exit_cfg(inputs["config"]),
        D(inputs["tick_size"]),
    )  # fmt: skip


def call_on_adjustment(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.exits import on_adjustment

    return on_adjustment(
        _position(inputs["position"]), D(inputs["rederived_high_since"]), _bar(inputs["bar"]),
        _exit_cfg(inputs["config"]), D(inputs["tick_size"]),
    )  # fmt: skip


def call_stop_fill(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.exits import stop_fill

    return stop_fill(_bar(inputs["bar"]), D(inputs["stop"]))


def call_fill_day_stop(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.exits import fill_day_stop

    return fill_day_stop(_position(inputs["position"]), _bar(inputs["bar"]))


def call_manage(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.exits import manage

    return manage(
        _position(inputs["position"]), _bar(inputs["bar"]),
        blank_sessions=int(inputs["blank_sessions"]), config=_exit_cfg(inputs["config"]),
    )  # fmt: skip


def call_r_multiple(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.exits import r_multiple

    return r_multiple(D(inputs["entry"]), D(inputs["initial"]), D(inputs["exit_price"]))


def _exit_cases() -> list[tuple[str, str, dict[str, Any]]]:  # noqa: PLR0915 - a flat table
    from baskfy_core.twt.config import ExitConfig

    out: list[tuple[str, str, dict[str, Any]]] = []
    mod = "baskfy_core.twt.exits."
    ecfg = _sub_doc(ExitConfig)
    t05, t01 = D("0.05"), D("0.01")

    for raw, why in ((None, "None: NSE cash equities' 0.05"), ("0.01", "the research's paisa"),
                     ("0.10", "a custom tick")):  # fmt: skip
        out.append((mod + "tick", why, {"tick_inr": raw}))

    tf = mod + "tick_floor"
    for price, size, why in (
        ("80.00", t05, "already on the tick"),
        ("80.04", t05, "rounds down, never up"),
        ("80.049999", t05, "just under a tick"),
        ("329.88", t05, "329.88 -> 329.85"),
        ("329.88", t01, "on the paisa"),
        ("0.04", t05, "below one tick: zero"),
        ("-1.03", t05, "a negative price rounds toward zero (ROUND_DOWN)"),
    ):
        out.append((tf, why, {"price": price, "tick_size": size}))

    ist = mod + "initial_stop"
    for fill, cfg, size, why in (
        ("100", ecfg, t05, "20% below 100: 80"),
        ("412.35", ecfg, t05, "412.35 x 0.8 = 329.88 -> 329.85"),
        ("412.35", ecfg, t01, "on the research's paisa: 329.88"),
        ("123.47", _sub_doc(ExitConfig, stop_pct=D("15")), t05, "a 15% stop"),
        ("0.30", ecfg, t05, "a penny price: 0.24 -> 0.20"),
        ("100", _sub_doc(ExitConfig, stop_pct=D("0")), t05, "a 0% stop: at the fill"),
    ):
        out.append((ist, why, {"fill_price": fill, "config": cfg, "tick_size": size}))

    sb = mod + "stop_is_below_entry"
    for fill, stop, why in (
        ("100", "80", "below: true"),
        ("100", "100", "equal: false"),
        ("100", "100.05", "above: false"),
        ("100", "0", "zero: false"),
        ("100", "-5", "negative: false"),
        ("0.05", "0.04", "a penny below a penny: true"),
    ):
        out.append((sb, why, {"fill_price": fill, "stop": stop}))

    tr = mod + "trail_from"
    for high, cfg, why in (
        ("150", ecfg, "20% under 150"),
        ("412.37", ecfg, "unrounded: the floor is the clamp's job"),
        ("150", _sub_doc(ExitConfig, trail_pct=D("25")), "a 25% trail"),
    ):
        out.append((tr, why, {"high_since": high, "config": cfg}))

    cl = mod + "clamp_under_close"
    for raw, close, size, why in (
        ("120", "140", t05, "well under the close: floored"),
        ("120.03", "140", t05, "floored to 120.00"),
        ("139.99", "140", t05, "above close x 0.9999: 139.986 -> 139.95"),
        ("140", "140", t05, "at the close: the 0.999 fallback, 139.86 -> 139.85"),
        ("150", "140", t05, "above the close: the fallback"),
        ("139.99", "140", t01, "on the paisa: 139.98"),
        ("120", "140", D("0.10"), "a 0.10 tick"),
    ):
        out.append((cl, why, {"raw_trigger": raw, "close": close, "config": ecfg,
                              "tick_size": size}))  # fmt: skip

    def pos(**over: Any) -> dict[str, Any]:
        base = {"instrument_id": 1, "entry_date": dt.date(2024, 3, 1), "fill_price": "100",
                "quantity": 250, "stop_price": "80", "initial_stop": "80", "high_since": "100"}
        return {**base, **over}  # fmt: skip

    def bar(day: int = 4, o: str | None = "120", h: str | None = "125", lo: str | None = "118",
            c: str | None = "122") -> dict[str, Any]:  # fmt: skip
        return {"session": dt.date(2024, 3, day), "open": o, "high": h, "low": lo, "close": c}

    ra = mod + "ratchet"
    for p, b, size, why in (
        (pos(), bar(), t05, "a new high of 125: trigger 100, raises from 80"),
        (pos(high_since="150", stop_price="120"), bar(), t05,
         "a lower high: high_since stays 150, trigger 120 does not raise"),
        (pos(), bar(c=None, o=None, h=None, lo=None), t05, "no bar: None"),
        (pos(high_since="130"), bar(h=None), t05, "a bar with no high: high_since kept"),
        (pos(high_since="200", stop_price="150"), bar(o="155", h="156", lo="152", c="153"), t05,
         "the trail sits at the close: clamped under it, a raise"),
        (pos(high_since="200", stop_price="155.10"), bar(o="155", h="156", lo="152", c="155.12"),
         t05, "the clamp lands below the resting stop: clamped_below_stop, stop_in_force kept"),
        (pos(high_since="100.07"), bar(h="100.06", c="99"), t01, "on the paisa"),
        (pos(), bar(h="100", c="100"), t05, "trigger equal to the stop: no raise"),
    ):  # fmt: skip
        out.append((ra, why, {"position": p, "bar": b, "config": ecfg, "tick_size": size}))

    oa = mod + "on_adjustment"
    for p, high, b, why in (
        (pos(stop_price="120", high_since="150"), "200", bar(c="190"),
         "a bonus re-derives a higher high: emit_raise, no alert"),
        (pos(stop_price="120", high_since="150"), "75", bar(c="70"),
         "a 1:2 split halves the high: below the resting stop, alert only"),
        (pos(stop_price="120", high_since="150"), "150", bar(c="140"),
         "re-derived trigger equals the resting one: alert, no raise"),
        (pos(stop_price="120", high_since="150"), "150", bar(c=None),
         "no bar: alert, the resting stop kept"),
        (pos(stop_price="80"), "200", bar(c="150"),
         "the trail above the close: clamped under it, a raise"),
    ):  # fmt: skip
        out.append((oa, why, {"position": p, "rederived_high_since": high, "bar": b,
                              "config": ecfg, "tick_size": t05}))  # fmt: skip

    sf = mod + "stop_fill"
    for b, stop, why in (
        (bar(o="78", lo="75"), "80", "gapped below: STOP_GAP at the open"),
        (bar(o="80", lo="79"), "80", "opened exactly at the stop: STOP_GAP"),
        (bar(o="90", lo="79.95"), "80", "traded through: STOP_HIT at the stop"),
        (bar(o="90", lo="80"), "80", "low exactly at the stop: STOP_HIT"),
        (bar(o="90", lo="80.05"), "80", "not reached: None"),
        (bar(o=None, lo="70"), "80", "no open, low through: STOP_HIT"),
        (bar(o=None, lo=None), "80", "nothing printed: None"),
    ):
        out.append((sf, why, {"bar": b, "stop": stop}))

    fd = mod + "fill_day_stop"
    entry_day = dict(day=1)
    for p, b, why in (
        (pos(), bar(**entry_day, o="100", lo="79", c="85"), "the entry session's low hits: STOP_DAY0 at the stop"),
        (pos(), bar(**entry_day, o="100", lo="80", c="85"), "low exactly at the stop: STOP_DAY0"),
        (pos(), bar(**entry_day, o="100", lo="80.05", c="85"), "low above the stop: None"),
        (pos(), bar(o="100", lo="70", c="85"), "a later session: None (not the fill day)"),
        (pos(), bar(**entry_day, o="78", lo="70", c="75"), "opened below the stop: at the open"),
        (pos(), bar(**entry_day, o="100", lo=None, c="85"), "no low: None"),
    ):  # fmt: skip
        out.append((fd, why, {"position": p, "bar": b}))

    mg = mod + "manage"
    blank = bar(o=None, h=None, lo=None, c=None)
    for p, b, blanks, why in (
        (pos(), blank, 5, "no bar for 5 sessions: WRITE_OFF, NO_BAR"),
        (pos(), blank, 7, "no bar for 7 sessions: WRITE_OFF"),
        (pos(), blank, 4, "no bar for 4 sessions: HOLD"),
        (pos(), bar(**entry_day, o="100", lo="79", c="85"), 0, "the fill-day rule first: STOP_DAY0"),
        (pos(stop_price="120"), bar(o="110", lo="105", c="108"), 0, "a gap through the stop: STOP_GAP"),
        (pos(stop_price="120"), bar(o="125", lo="119", c="121"), 0, "traded through: STOP_HIT"),
        (pos(stop_price="120"), bar(o="125", lo="121", c="124"), 0, "above the stop: HOLD"),
        (pos(), bar(**entry_day, o="100", h="104", lo="95", c="101"), 0,
         "the entry session above the stop: HOLD"),
    ):  # fmt: skip
        out.append((mg, why, {"position": p, "bar": b, "blank_sessions": blanks,
                              "config": ecfg}))  # fmt: skip
    out.append((mg, "no_bar_sessions=3 writes off at 3",
                {"position": pos(), "bar": blank, "blank_sessions": 3,
                 "config": _sub_doc(ExitConfig, no_bar_sessions=3)}))  # fmt: skip

    rm = mod + "r_multiple"
    for entry, initial, exit_price, why in (
        ("100", "80", "140", "+2R"),
        ("100", "80", "80", "-1R at the stop"),
        ("100", "80", "100", "0R"),
        ("100", "100", "120", "no risk: None"),
        ("100", "110", "120", "negative risk: None"),
        ("412.35", "329.85", "500.10", "rounded half-even to 2 dp: 1.06"),
        ("100", "70", "101", "1/30 R: 0.03"),
    ):
        out.append((rm, why, {"entry": entry, "initial": initial, "exit_price": exit_price}))
    return out


# ---------------------------------------------------------------------------
# sleeve
# ---------------------------------------------------------------------------


def call_sleeve_value(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.sleeve import MarkSource, OpenPositionValue, sleeve_value

    positions = [
        OpenPositionValue(
            instrument_id=int(p["instrument_id"]), quantity_open=int(p["quantity_open"]),
            entry_avg=D(p["entry_avg"]), mark=D(p["mark"]), mark_source=MarkSource(p["mark_source"]),
        )
        for p in inputs["open_positions"]
    ]  # fmt: skip
    value = sleeve_value(
        capital_inr=D(inputs["capital_inr"]), realised_inr=D(inputs["realised_inr"]),
        open_positions=positions,
    )  # fmt: skip
    return {
        "value": value,
        "derived": {
            "cash_inr": value.cash_inr,
            "equity_inr": value.equity_inr,
            "cash_available_inr": value.cash_available_inr,
            "open_exposure_inr": value.open_exposure_inr,
            "unrealised_inr": value.unrealised_inr,
            "has_capital": value.has_capital,
            "quantize": value.quantize(),
        },
        "positions": [
            {"cost": p.cost, "value": p.value, "is_stale_mark": p.is_stale_mark} for p in positions
        ],
    }


def _sleeve_cases() -> list[tuple[str, str, dict[str, Any]]]:
    out: list[tuple[str, str, dict[str, Any]]] = []
    sv = "baskfy_core.twt.sleeve.sleeve_value"

    def p(iid: int, qty: int, entry: str, mark: str, source: str = "SESSION_CLOSE") -> dict[str, Any]:
        return {"instrument_id": iid, "quantity_open": qty, "entry_avg": entry, "mark": mark,
                "mark_source": source}  # fmt: skip

    for why, capital, realised, positions in (
        ("a fresh sleeve: all cash", "2500000", "0", []),
        ("a sleeve at zero plans nothing: has_capital false", "0", "0", []),
        ("two open positions marked at the session close", "2500000", "15234.50",
         [p(1, 600, "412.35", "455.10"), p(2, 1200, "205.40", "198.75")]),
        ("stale marks: LAST_KNOWN_CLOSE and ENTRY", "2500000", "0",
         [p(1, 600, "412.35", "455.10", "LAST_KNOWN_CLOSE"), p(3, 100, "999.99", "999.99", "ENTRY")]),
        ("realised losses beyond capital: cash negative, cash_available floored at 0",
         "100000", "-150000", [p(1, 100, "500", "400")]),
        ("sub-paise arithmetic: quantize rounds every rupee to the paise", "1000000.005",
         "12.3456", [p(1, 3, "333.3333", "333.3366")]),
        ("everything deployed: cash zero", "100000", "0", [p(1, 1000, "100", "110")]),
    ):  # fmt: skip
        out.append((sv, why, {"capital_inr": capital, "realised_inr": realised,
                              "open_positions": positions}))  # fmt: skip
    return out


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


def _book(raw: dict[str, Any]) -> Any:
    from baskfy_core.twt.plan import BookState, NakedPosition, RatchetDue, SellDue

    return BookState(
        open_instrument_ids=frozenset(int(i) for i in raw["open_instrument_ids"]),
        open_exposure_inr=D(raw["open_exposure_inr"]),
        cash_available_inr=D(raw["cash_available_inr"]),
        entries_already_this_session=int(raw["entries_already_this_session"]),
        positions_naked_of_gtt=tuple(
            NakedPosition(int(n["instrument_id"]), n["symbol"], int(n["quantity"]),
                          D(n["stop_price"]), n["position_id"])
            for n in raw["positions_naked_of_gtt"]
        ),
        ratchets_due=tuple(
            RatchetDue(int(r["instrument_id"]), r["symbol"], int(r["quantity"]),
                       D(r["gtt_trigger"]), D(r["next_trigger"]), _date(r["next_trigger_for"]),
                       D(r["high_since"]), r["note"], r["position_id"])
            for r in raw["ratchets_due"]
        ),
        sells_due=tuple(
            SellDue(int(s["instrument_id"]), s["symbol"], int(s["quantity"]), s["reason"],
                    _date(s["decided_for"]), s["note"], s["position_id"])
            for s in raw["sells_due"]
        ),
        open_entry_counts={int(k): int(v) for k, v in raw["open_entry_counts"].items()},
    )  # fmt: skip


def _candidate(raw: dict[str, Any]) -> Any:
    from baskfy_core.twt.plan import Candidate, EntryBar

    bar = raw["entry_bar"]
    return Candidate(
        instrument_id=int(raw["instrument_id"]), symbol=raw["symbol"],
        signal_date=_date(raw["signal_date"]), rank_key=D(raw["rank_key"]),
        turnover_avg_inr=_d(raw["turnover_avg_inr"]), entry_price=D(raw["entry_price"]),
        stop_reference_price=D(raw["stop_reference_price"]),
        entry_bar=None if bar is None else EntryBar(
            session=_date(bar["session"]), open=_d(bar["open"]), limit_locked=bool(bar["limit_locked"])
        ),
    )  # fmt: skip


def _line(raw: dict[str, Any]) -> Any:
    from baskfy_core.twt.plan import LineKind, PlanLine
    from baskfy_core.twt.sizing import SizeCap

    return PlanLine(
        kind=LineKind(raw["kind"]), instrument_id=int(raw["instrument_id"]), symbol=raw["symbol"],
        quantity=int(raw["quantity"]), entry_price=_d(raw["entry_price"]),
        stop_price=_d(raw["stop_price"]), previous_stop=_d(raw["previous_stop"]),
        high_since=_d(raw["high_since"]), value_inr=D(raw["value_inr"]),
        cap=None if raw["cap"] is None else SizeCap(raw["cap"]), note=raw["note"],
        position_id=raw["position_id"],
    )  # fmt: skip


def call_client_id_for(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.plan import LineKind, client_id_for

    return client_id_for(inputs["plan_id"], inputs["symbol"], LineKind(inputs["kind"]))


def call_stop_for(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.plan import stop_for

    return stop_for(D(inputs["reference_price"]), _twt_config(inputs["config"]), D(inputs["tick"]))


def call_gtt_limit_price(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.plan import gtt_limit_price

    return gtt_limit_price(D(inputs["trigger"]), _twt_config(inputs["config"]), _d(inputs["tick"]))


def call_build_entries(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.config import Gate
    from baskfy_core.twt.plan import build_entries

    lines, skips = build_entries(
        [_candidate(c) for c in inputs["candidates"]],
        gate=Gate(inputs["gate"]), equity=D(inputs["equity"]), book=_book(inputs["book"]),
        config=_twt_config(inputs["config"]), slot_multiplier=D(inputs["slot_multiplier"]),
        max_open_positions=inputs["max_open_positions"], tick=_d(inputs["tick"]),
    )  # fmt: skip
    return {"lines": lines, "skips": skips}


def call_exit_lines(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.plan import exit_lines

    return exit_lines(_book(inputs["book"]), _date(inputs["session"]))


def call_assemble(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.config import Gate
    from baskfy_core.twt.plan import Skipped, SkipReason, assemble

    plan = assemble(
        _date(inputs["session"]), Gate(inputs["gate"]), D(inputs["equity"]),
        [_line(x) for x in inputs["exits"]], [_line(x) for x in inputs["entries"]],
        [Skipped(int(s["instrument_id"]), s["symbol"], SkipReason(s["reason"]), s["detail"])
         for s in inputs["skips"]],
        inputs["source"],
    )  # fmt: skip
    return {
        "plan": plan,
        "derived": {
            "entries": plan.entries,
            "total_new_exposure_inr": plan.total_new_exposure_inr,
            "plan_hash": plan.plan_hash(),
            "canonical": [line.canonical() for line in plan.lines],
        },
    }


def _empty_book(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "open_instrument_ids": [], "open_exposure_inr": D("0"),
        "cash_available_inr": D("2500000"), "entries_already_this_session": 0,
        "positions_naked_of_gtt": [], "ratchets_due": [], "sells_due": [],
        "open_entry_counts": {},
    }  # fmt: skip
    return {**base, **over}


def _plan_cases() -> list[tuple[str, str, dict[str, Any]]]:  # noqa: PLR0915 - a flat table
    out: list[tuple[str, str, dict[str, Any]]] = []
    mod = "baskfy_core.twt.plan."
    cfg = _cfg_doc()

    ci = mod + "client_id_for"
    for kind in ("BUY_AT_OPEN", "ARM_GTT", "RAISE_GTT_STOP", "SELL_AT_OPEN"):
        out.append((ci, f"{kind}: {'plan_id:symbol' if kind == 'BUY_AT_OPEN' else 'plan_id:symbol:kind'}",
                    {"plan_id": "tw-20240520-ev", "symbol": "M&M", "kind": kind}))  # fmt: skip

    sf = mod + "stop_for"
    for ref, tick, c, why in (
        ("412.35", "0.05", cfg, "20% under the reference, floored to 0.05"),
        ("412.35", "0.01", cfg, "on the paisa"),
        ("57.13", "0.05", _cfg_doc(exits={"stop_pct": D("25")}), "a 25% stop from the config"),
    ):
        out.append((sf, why, {"reference_price": ref, "config": c, "tick": tick}))

    gl = mod + "gtt_limit_price"
    for trig, tick, c, why in (
        ("329.85", None, cfg, "3% under the trigger, default tick: 319.95"),
        ("329.85", "0.01", cfg, "on the paisa: 319.95"),
        ("100", None, cfg, "97.00"),
        ("12.34", None, _cfg_doc(exits={"gtt_limit_fraction": D("0.995")}), "the weekly 0.995"),
    ):
        out.append((gl, why, {"trigger": trig, "config": c, "tick": tick}))

    be = mod + "build_entries"
    sig = dt.date(2024, 5, 20)

    def cand(iid: int, sym: str, rank: str, *, turn: str | None = "300000000",
             price: str = "412.35", ref: str | None = None,
             bar: dict[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
        return {"instrument_id": iid, "symbol": sym, "signal_date": sig, "rank_key": rank,
                "turnover_avg_inr": turn, "entry_price": price,
                "stop_reference_price": ref or price, "entry_bar": bar}  # fmt: skip

    def entry_bar(o: str | None, locked: bool = False) -> dict[str, Any]:
        return {"session": dt.date(2024, 5, 21), "open": o, "limit_locked": locked}

    def build(why: str, cands: list[dict[str, Any]], **over: Any) -> None:
        inputs = {"candidates": cands, "gate": "OPEN", "equity": D("2500000"),
                  "book": _empty_book(), "config": cfg, "slot_multiplier": D("1"),
                  "max_open_positions": None, "tick": None}  # fmt: skip
        inputs.update(over)
        out.append((be, why, inputs))

    three = [cand(1, "AAA", "90000000"), cand(2, "BBB", "120000000", price="205.40"),
             cand(3, "CCC", "60000000", price="57.13")]  # fmt: skip
    build("three signals, ranked by rank_key desc: three lines", three)
    build("the gate SHUT: every signal GATE_SHUT", three, gate="SHUT")
    build("ties on rank_key break by symbol ascending",
          [cand(1, "ZED", "50000000"), cand(2, "ABC", "50000000"), cand(3, "MID", "50000000")])
    build("pyramiding off: a held name is ALREADY_HELD",
          three, book=_empty_book(open_instrument_ids=[2]),
          config=_cfg_doc(sizing={"pyramiding": False}))
    build("pyramiding on: one open entry in BBB, a second is allowed; CCC at the cap of 2",
          three, book=_empty_book(open_instrument_ids=[2, 3], open_entry_counts={2: 1, 3: 2}))
    build("held with no entry counts: open_instrument_ids counts one each",
          three, book=_empty_book(open_instrument_ids=[1, 2]),
          config=_cfg_doc(sizing={"max_entries_per_name": 1}))
    build("below the liquidity floor, and no turnover reading: BELOW_LIQUIDITY_FLOOR",
          [cand(1, "THIN", "9", turn="19999999.99"), cand(2, "NULL", "8", turn=None),
           cand(3, "EXACT", "7", turn="20000000")])
    build("entry bar: no open is NO_BAR, open==high==low is LOCKED_UPPER_CIRCUIT",
          [cand(1, "NOBAR", "9", bar=entry_bar(None)),
           cand(2, "LOCK", "8", bar=entry_bar("420", locked=True)),
           cand(3, "FILL", "7", bar=entry_bar("415.00"), price="415.00")])
    build("the session cap: two already sent, one more line, then SESSION_CAP", three,
          book=_empty_book(entries_already_this_session=2))
    build("slots: nine held, one line, then SLOTS_FULL", three,
          book=_empty_book(open_entry_counts={100 + i: 1 for i in range(9)},
                           open_instrument_ids=[100 + i for i in range(9)],
                           open_exposure_inr=D("1800000"), cash_available_inr=D("700000")))
    build("max_open_positions 12 governs over ten slots", three,
          book=_empty_book(open_entry_counts={100 + i: 1 for i in range(10)},
                           open_instrument_ids=[100 + i for i in range(10)],
                           open_exposure_inr=D("1500000"), cash_available_inr=D("1000000")),
          max_open_positions=12)
    build("equity zero: NO_SLEEVE_CAPITAL", three, equity=D("0"))
    build("a 0% stop: STOP_NOT_BELOW_ENTRY where the price sits on the tick; 57.13 floors to "
          "57.10 and is sized", three,
          config=_cfg_doc(exits={"stop_pct": D("0")}))
    build("little cash: the first line is cash-capped, the rest BELOW_MIN_TRADE_VALUE", three,
          book=_empty_book(cash_available_inr=D("200000")))
    build("exposure: open exposure near equity makes a line EXPOSURE_FULL", three,
          book=_empty_book(open_exposure_inr=D("2300000"), cash_available_inr=D("400000")))
    build("a turnover-capped line is smaller, with a note, not a skip",
          [cand(1, "FLOOR", "9", turn="20000000")])
    build("first live: slot_multiplier 0.5", three, slot_multiplier=D("0.5"))
    build("the paisa tick and a stop reference below the preview price",
          [cand(1, "AAA", "9", price="412.35", ref="410.07")], tick=D("0.01"))
    build("no candidates: nothing", [])

    el = mod + "exit_lines"
    session = dt.date(2024, 5, 20)
    other = dt.date(2024, 5, 17)

    def naked(iid: int, sym: str, pid: int | None = None) -> dict[str, Any]:
        return {"instrument_id": iid, "symbol": sym, "quantity": 300, "stop_price": D("329.85"),
                "position_id": pid}  # fmt: skip

    def due(iid: int, sym: str, gtt: str, nxt: str, when: dt.date, note: str = "",
            pid: int | None = None) -> dict[str, Any]:  # fmt: skip
        return {"instrument_id": iid, "symbol": sym, "quantity": 300, "gtt_trigger": gtt,
                "next_trigger": nxt, "next_trigger_for": when, "high_since": "520.10",
                "note": note, "position_id": pid}  # fmt: skip

    def sell(iid: int, sym: str, qty: int, reason: str, when: dt.date, note: str = "",
             pid: int | None = None) -> dict[str, Any]:  # fmt: skip
        return {"instrument_id": iid, "symbol": sym, "quantity": qty, "reason": reason,
                "decided_for": when, "note": note, "position_id": pid}  # fmt: skip

    out.append((el, "an empty book: no lines", {"book": _empty_book(), "session": session}))
    out.append((el, "naked positions become ARM_GTT, with and without a position_id",
                {"book": _empty_book(positions_naked_of_gtt=[naked(1, "AAA"), naked(2, "BBB", 77)]),
                 "session": session}))  # fmt: skip
    out.append((el, "ratchets: higher and for this session is a line; equal, lower or stale is not",
                {"book": _empty_book(ratchets_due=[
                    due(1, "UP", "400.00", "416.05", session),
                    due(2, "SAME", "400.00", "400.00", session),
                    due(3, "DOWN", "400.00", "399.95", session),
                    due(4, "STALE", "400.00", "430.00", other),
                    due(5, "NOTED", "100.00", "101.00", session, note="breakeven", pid=9),
                ]), "session": session}))  # fmt: skip
    out.append((el, "sales: this session's partial and MA trail; a stale one and a zero quantity "
                "are not lines",
                {"book": _empty_book(sells_due=[
                    sell(1, "PART", 100, "PARTIAL", session),
                    sell(2, "TRAIL", 300, "MA_TRAIL", session, note="close under MA10", pid=4),
                    sell(3, "OLD", 300, "MA_TRAIL", other),
                    sell(4, "ZERO", 0, "PARTIAL", session),
                ]), "session": session}))  # fmt: skip
    out.append((el, "all three kinds at once: sells, then arms, then raises (input order kept)",
                {"book": _empty_book(
                    sells_due=[sell(9, "ZSELL", 50, "PARTIAL", session)],
                    positions_naked_of_gtt=[naked(8, "YARM")],
                    ratchets_due=[due(7, "XRAISE", "90.00", "95.00", session)]),
                 "session": session}))  # fmt: skip

    asm = mod + "assemble"

    def line(kind: str, iid: int, sym: str, qty: int, **over: Any) -> dict[str, Any]:
        base = {"kind": kind, "instrument_id": iid, "symbol": sym, "quantity": qty,
                "entry_price": None, "stop_price": None, "previous_stop": None,
                "high_since": None, "value_inr": D("0"), "cap": None, "note": "",
                "position_id": None}  # fmt: skip
        return {**base, **over}

    buy_a = line("BUY_AT_OPEN", 1, "AAA", 606, entry_price=D("412.35"), stop_price=D("329.85"),
                 value_inr=D("249884.10"), cap="SLOT", note="buy")  # fmt: skip
    buy_b = line("BUY_AT_OPEN", 2, "BBB", 1217, entry_price=D("205.40"), stop_price=D("164.30"),
                 value_inr=D("249971.80"), cap="SLOT")  # fmt: skip
    arm = line("ARM_GTT", 3, "CCC", 300, stop_price=D("50.00"))
    raise_ = line("RAISE_GTT_STOP", 4, "DDD", 100, stop_price=D("416.05"),
                  previous_stop=D("400.00"), high_since=D("520.10"), position_id=12)  # fmt: skip
    sell_ = line("SELL_AT_OPEN", 5, "EEE", 100, position_id=13)
    skip = {"instrument_id": 6, "symbol": "FFF", "reason": "SESSION_CAP", "detail": "3 a session"}
    out.append((asm, "every kind: ordered SELL, ARM, RAISE, BUY then symbol; hash over lines",
                {"session": session, "gate": "OPEN", "equity": D("2500000"),
                 "exits": [raise_, arm, sell_], "entries": [buy_b, buy_a], "skips": [skip],
                 "source": "EVENING"}))  # fmt: skip
    out.append((asm, "an empty plan: the hash of no lines",
                {"session": session, "gate": "SHUT", "equity": D("0"), "exits": [],
                 "entries": [], "skips": [], "source": "EVENING"}))  # fmt: skip
    out.append((asm, "a MORNING rebuild of the entries alone",
                {"session": session, "gate": "OPEN", "equity": D("2500000"), "exits": [],
                 "entries": [buy_a, buy_b], "skips": [], "source": "MORNING"}))  # fmt: skip
    out.append((asm, "the same lines under the other gate hash differently",
                {"session": session, "gate": "SHUT", "equity": D("2500000"),
                 "exits": [raise_, arm, sell_], "entries": [buy_b, buy_a], "skips": [skip],
                 "source": "EVENING"}))  # fmt: skip
    return out


# ---------------------------------------------------------------------------
# drift and published
# ---------------------------------------------------------------------------


def _num(value: Any) -> Any:
    """A JSON number is a float; a string is a Decimal (``compare`` takes either)."""
    return Decimal(value) if isinstance(value, str) else float(value)


def call_compare(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.drift import compare
    from baskfy_core.twt.published import PublishedStudy

    drift = compare(
        cagr_pct=_num(inputs["cagr_pct"]), max_drawdown_pct=_num(inputs["max_drawdown_pct"]),
        trades=int(inputs["trades"]), study=PublishedStudy(**inputs["study"]),
        threshold=D(inputs["threshold"]),
    )  # fmt: skip
    return {"drift": drift, "to_json": drift.to_json()}


def call_published(_inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.published import PUBLISHED

    return {"study": PUBLISHED, "cagr": PUBLISHED.cagr}


def _drift_cases() -> list[tuple[str, str, dict[str, Any]]]:
    from baskfy_core.twt.drift import DRIFT_CAGR_POINTS
    from baskfy_core.twt.published import PUBLISHED

    out: list[tuple[str, str, dict[str, Any]]] = []
    cp = "baskfy_core.twt.drift.compare"
    study = _golden()._enc(PUBLISHED)
    thr = DRIFT_CAGR_POINTS
    for cagr, dd, trades, threshold, why in (
        (20.92, -24.7, 164, thr, "the study itself: zero deltas, not flagged"),
        (22.5, -27.0, 169, thr, "the 5-crore experiment: +1.58 points, flagged"),
        (19.92, -24.7, 160, thr, "exactly one point below: not flagged (strictly above)"),
        (19.915, -24.705, 150, thr, "19.915 rounds half-up to 19.92: not flagged"),
        (19.9149, -30.0, 100, thr, "19.9149 rounds to 19.91: flagged below"),
        ("18.4567", "-26.125", 140, thr, "Decimal inputs, half-up to 18.46 and -26.13"),
        (21.5, -20.0, 200, D("0.5"), "a 0.5-point threshold flags +0.58"),
    ):
        out.append((cp, why, {"cagr_pct": cagr, "max_drawdown_pct": dd, "trades": trades,
                              "study": study, "threshold": threshold}))  # fmt: skip
    custom = {**study, "cagr_pct": 10.0, "max_drawdown_pct": -10.0, "trades": 10}
    out.append((cp, "a different study record", {"cagr_pct": 10.004, "max_drawdown_pct": -10.0,
                                                 "trades": 12, "study": custom,
                                                 "threshold": thr}))  # fmt: skip
    out.append(("baskfy_core.twt.published.PUBLISHED", "the study's record and its Decimal cagr", {}))
    return out


# ---------------------------------------------------------------------------
# backtest
# ---------------------------------------------------------------------------

MATRICES = ("open", "high", "low", "close", "turnover_inr", "turnover_avg_20")


def _panel(raw: dict[str, Any]) -> Any:
    import numpy as np

    from baskfy_core.twt.backtest import Panel

    def matrix(rows: list[list[Any]]) -> Any:
        n = len(raw["instrument_ids"])
        m = len(raw["sessions"])
        out = np.full((n, m), np.nan)
        for i, row in enumerate(rows):
            for j, value in enumerate(row):
                if value is not None:
                    out[i, j] = float(value)
        return out

    signal = np.zeros((len(raw["instrument_ids"]), len(raw["sessions"])), dtype=bool)
    for i, row in enumerate(raw["signal"]):
        for j, value in enumerate(row):
            signal[i, j] = bool(value)
    return Panel(
        symbols=tuple(raw["symbols"]),
        instrument_ids=tuple(int(i) for i in raw["instrument_ids"]),
        sessions=tuple(_date(s) for s in raw["sessions"]),
        signal=signal,
        **{name: matrix(raw[name]) for name in MATRICES},
    )


def _params(raw: dict[str, Any]) -> Any:
    from baskfy_core.twt.backtest import BacktestParams

    return BacktestParams(
        sleeve_inr=D(raw["sleeve_inr"]), start=_date(raw["start"]), end=_date(raw["end"]),
        cost_pct_per_side=D(raw["cost_pct_per_side"]), tick=D(raw["tick"]),
        config=_twt_config(raw["config"]), label=raw["label"],
    )  # fmt: skip


def _result(raw: dict[str, Any]) -> Any:
    from baskfy_core.twt.backtest import BacktestResult, BacktestTrade
    from baskfy_core.twt.exits import ExitReason
    from baskfy_core.twt.sizing import SizeCap

    trades = tuple(
        BacktestTrade(
            symbol=t["symbol"], instrument_id=int(t["instrument_id"]),
            entry_date=_date(t["entry_date"]), exit_date=_date(t["exit_date"]),
            entry_price=D(t["entry_price"]), exit_price=D(t["exit_price"]),
            quantity=int(t["quantity"]), pnl_inr=D(t["pnl_inr"]), return_pct=D(t["return_pct"]),
            r_multiple=_d(t["r_multiple"]), hold_sessions=int(t["hold_sessions"]),
            reason=ExitReason(t["reason"]), cap=None if t["cap"] is None else SizeCap(t["cap"]),
        )
        for t in raw["trades"]
    )  # fmt: skip
    return BacktestResult(
        params=_params(raw["params"]), trades=trades,
        sessions=tuple(_date(s) for s in raw["sessions"]),
        equity=tuple(D(e) for e in raw["equity"]),
        open_positions=tuple(int(n) for n in raw["open_positions"]),
        skipped={k: int(v) for k, v in raw["skipped"].items()},
        clamped_below_stop=int(raw["clamped_below_stop"]),
    )  # fmt: skip


def call_panel_from_frame(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.backtest import panel_from_frame

    return panel_from_frame(_frame(inputs["indicated"]), inputs["signal_column"])


def call_gate_vector(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.backtest import gate_vector

    return gate_vector(_frame(inputs["breadth"]), tuple(_date(s) for s in inputs["sessions"]))


def call_run_backtest(inputs: dict[str, Any]) -> Any:
    import numpy as np

    from baskfy_core.twt.backtest import run_backtest

    gate = np.array([bool(g) for g in inputs["gate_open"]], dtype=bool)
    return run_backtest(_panel(inputs["panel"]), gate, _params(inputs["params"]))


def call_summarise(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.backtest import summarise

    return summarise(_result(inputs["result"]))


def call_yearly(inputs: dict[str, Any]) -> Any:
    from baskfy_core.twt.backtest import yearly

    return yearly(_result(inputs["result"]))


def _params_doc(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "sleeve_inr": D("1000000"), "start": None, "end": None,
        "cost_pct_per_side": D("0.25"), "tick": D("0.05"), "config": _cfg_doc(), "label": "full",
    }  # fmt: skip
    base.update(over)
    return base


Ohlc = tuple[float, float, float, float] | None


def _panel_doc(
    names: list[tuple[int, str, list[Ohlc]]],
    signals: dict[int, list[int]],
    *,
    start: dt.date = START,
    turnover: dict[int, float] | None = None,
    turnover_avg: dict[int, float | None] | None = None,
) -> dict[str, Any]:
    """A Panel doc. ``names`` is (id, symbol, bars per session); ``signals[id]`` lists session
    columns where the name signals. ``turnover[id]`` is the per-session turnover (rank key)."""
    m = len(names[0][2])
    sessions = _weekdays(m, start)
    doc: dict[str, Any] = {
        "symbols": [s for _, s, _ in names],
        "instrument_ids": [i for i, _, _ in names],
        "sessions": sessions,
        "signal": [],
        **{k: [] for k in MATRICES},
    }
    for iid, _, bars in names:
        for k, idx in (("open", 0), ("high", 1), ("low", 2), ("close", 3)):
            doc[k].append([None if b is None else b[idx] for b in bars])
        t = (turnover or {}).get(iid, 5e7)
        doc["turnover_inr"].append([None if b is None else t for b in bars])
        ta = (turnover_avg or {}).get(iid, 1e9)
        doc["turnover_avg_20"].append([ta for _ in bars])
        doc["signal"].append([j in signals.get(iid, []) for j in range(m)])
    return doc


def _flat(price: float, n: int) -> list[Ohlc]:
    return [(price, price * 1.01, price * 0.99, price) for _ in range(n)]


def _path(closes: list[float]) -> list[Ohlc]:
    """Bars around a close path: open at the previous close, a 1% range around the move."""
    out: list[Ohlc] = []
    prev = closes[0]
    for c in closes:
        o = prev
        out.append((round(o, 2), round(max(o, c) * 1.01, 2), round(min(o, c) * 0.99, 2), round(c, 2)))
        prev = c
    return out


def _backtest_cases() -> tuple[list[tuple[str, str, dict[str, Any]]], list[dict[str, Any]]]:  # noqa: PLR0915
    """The cases, and the run_backtest inputs whose results feed summarise and yearly."""
    out: list[tuple[str, str, dict[str, Any]]] = []
    mod = "baskfy_core.twt.backtest."

    # -- panel_from_frame ---------------------------------------------------------------------
    pf = mod + "panel_from_frame"
    pcols = ["instrument_id", "symbol", "date", "open", "high", "low", "close", "turnover_inr",
             "turnover_avg_20", "entry_event", "signal_state"]  # fmt: skip
    d4 = _weekdays(4)
    prow = [
        [2, "BBB", d4[0], 50.0, 51.0, 49.0, 50.5, 5e6, None, False, None],
        [1, "AAA", d4[1], 10.0, 10.5, 9.5, 10.2, 1e6, 9e5, True, "SIGNAL"],
        [1, "AAA", d4[0], 10.1, 10.6, 9.9, 10.0, 1.1e6, 8e5, False, None],
        [2, None, d4[2], 51.0, 52.0, 50.0, 51.5, 5.2e6, 4e6, None, "SCAN_ONLY"],
        [1, "AAA", d4[3], 10.4, 10.9, 10.3, 10.8, 1.3e6, 1e6, True, "SIGNAL"],
        [3, "CCC", d4[3], 7.0, 7.0, 7.0, 7.0, 7e5, None, True, "SCAN_ONLY"],
    ]  # fmt: skip
    pdoc = _doc(pcols, SIG_DTYPES, prow)
    out.append((pf, "unsorted rows, gaps as NaN, entry_event as the signal",
                {"indicated": pdoc, "signal_column": "entry_event"}))  # fmt: skip
    out.append((pf, "signal_state: SIGNAL is true, SCAN_ONLY and null are not",
                {"indicated": pdoc, "signal_column": "signal_state"}))  # fmt: skip
    out.append((pf, "a signal column the frame lacks: all false",
                {"indicated": pdoc, "signal_column": "nope"}))  # fmt: skip
    nosym = [c for c in pcols if c not in ("symbol", "turnover_avg_20")]
    out.append((pf, "no symbol and no turnover_avg_20 column: empty symbols, an all-NaN matrix",
                {"indicated": _doc(nosym, SIG_DTYPES,
                                   [[r[pcols.index(c)] for c in nosym] for r in prow]),
                 "signal_column": "entry_event"}))  # fmt: skip
    out.append((pf, "an empty frame", {"indicated": _doc(pcols, SIG_DTYPES, []),
                                        "signal_column": "entry_event"}))  # fmt: skip

    # -- gate_vector ----------------------------------------------------------------------------
    gv = mod + "gate_vector"
    d5 = _weekdays(5)
    gdoc = _doc(["date", "gate"], {"date": "Date", "gate": "String"},
                [[d5[0], "OPEN"], [d5[1], "SHUT"], [d5[3], "OPEN"]])  # fmt: skip
    out.append((gv, "a session with no row is SHUT", {"breadth": gdoc, "sessions": d5}))
    out.append((gv, "sessions in a different order", {"breadth": gdoc,
                                                       "sessions": list(reversed(d5))}))
    out.append((gv, "no sessions", {"breadth": gdoc, "sessions": []}))
    out.append((gv, "an empty breadth frame: every session SHUT",
                {"breadth": _doc(["date", "gate"], {"date": "Date", "gate": "String"}, []),
                 "sessions": d5}))  # fmt: skip

    # -- run_backtest ---------------------------------------------------------------------------
    rb = mod + "run_backtest"
    runs: list[dict[str, Any]] = []

    def run(why: str, panel: dict[str, Any], gate: list[bool] | None = None,
            params: dict[str, Any] | None = None) -> None:  # fmt: skip
        m = len(panel["sessions"])
        inputs = {"panel": panel, "gate_open": gate if gate is not None else [True] * m,
                  "params": params or _params_doc()}  # fmt: skip
        out.append((rb, why, inputs))
        runs.append(inputs)

    up = [100.0, 100.0, 104, 110, 118, 126, 135, 150, 146, 141, 144, 140]
    run("one entry trails up and is liquidated at the end: END_OF_RUN, the ratchet rising",
        _panel_doc([(1, "RISE", _path(up)), (2, "FLAT", _flat(50.0, 12))], {1: [0]}))
    hit = [100.0, 100.0, 110, 125, 150, 140, 135, 128, 122, 118, 117, 116]
    run("the trail hit intraday: STOP_HIT at the trigger",
        _panel_doc([(1, "HIT", _path(hit))], {1: [0]}))
    gap = _path([100.0, 100.0, 110, 125, 150, 140, 138, 136, 135, 133, 131, 130])
    gap[9] = (100.0, 101.0, 98.0, 99.0)
    gap[10] = (99.0, 100.0, 98.0, 99.5)
    gap[11] = (99.5, 100.0, 99.0, 99.8)
    run("a gap through the trail: STOP_GAP at the open",
        _panel_doc([(1, "GAP", gap)], {1: [0]}))
    day0 = _flat(100.0, 8)
    day0[1] = (100.0, 101.0, 79.0, 85.0)
    run("the entry session's own low takes it out: STOP_DAY0 at the stop",
        _panel_doc([(1, "DAY0", day0), (2, "BG", _flat(30.0, 8))], {1: [0]}))
    gone: list[Ohlc] = _flat(100.0, 4) + [None] * 7
    run("a name stops printing: NO_BAR write-off at its last close after 5 blank sessions",
        _panel_doc([(1, "GONE", gone), (2, "BG", _flat(30.0, 11))], {1: [0]}))
    run("the gate shut on the signal session: counted gate_shut, then a later signal fills",
        _panel_doc([(1, "AAA", _flat(100.0, 8)), (2, "BBB", _flat(40.0, 8))],
                   {1: [0], 2: [0, 3]}),
        gate=[False, True, True, True, True, True, True, True])
    many = [(i, f"N{i}", _flat(20.0 + i, 6)) for i in range(1, 6)]
    run("five signals on one session: three fill (session cap), ranked by turnover",
        _panel_doc(many, {i: [0] for i in range(1, 6)},
                   turnover={1: 1e7, 2: 5e7, 3: 3e7, 4: 5e7, 5: 9e7}))
    run("max_slots 2: the third signal on a later session is SLOTS_FULL",
        _panel_doc(many, {1: [0], 2: [0], 3: [2], 4: [2]}),
        params=_params_doc(config=_cfg_doc(sizing={"max_slots": 2, "max_position_pct": D("60")})))
    blocked = _flat(30.0, 6)
    blocked[1] = (31.0, 31.0, 31.0, 31.0)
    nobar: list[Ohlc] = _flat(60.0, 6)
    nobar[1] = None
    run("entry session locked (o==h==l) and no bar: skipped locked and no_bar",
        _panel_doc([(1, "LOCK", blocked), (2, "NOBAR", nobar), (3, "OK", _flat(80.0, 6))],
                   {1: [0], 2: [0], 3: [0]}))
    run("a 60,000 sleeve: a 6,000 slot is BELOW_MIN_TRADE_VALUE with cash on hand -> turnover",
        _panel_doc([(1, "THIN", _flat(50.0, 6)), (2, "PRICY", _flat(70000.0, 6))],
                   {1: [0], 2: [0]}, turnover_avg={1: 5e5}),
        params=_params_doc(sleeve_inr=D("60000")))
    run("a 9,000 sleeve: cash refusals; a 0.05 open whose stop floors to zero is counted cash",
        _panel_doc([(1, "AAA", _flat(50.0, 6)), (2, "PENNY", _flat(0.05, 6))],
                   {1: [0], 2: [0]}, turnover={1: 9e7, 2: 1e7}),
        params=_params_doc(sleeve_inr=D("9000")))
    run("a penny name in a full sleeve: STOP_NOT_BELOW_ENTRY is counted cash",
        _panel_doc([(1, "PENNY", _flat(0.05, 6))], {1: [0]}))
    run("the research's parameters: paisa tick, zero cost, a start/end window",
        _panel_doc([(1, "RISE", _path(up)), (2, "HIT", _path(hit))], {1: [2], 2: [3]}),
        params=_params_doc(tick=D("0.01"), cost_pct_per_side=D("0"),
                           start=_weekdays(12)[2], end=_weekdays(12)[10], label="research"))
    run("no signal anywhere: no trades, a flat curve",
        _panel_doc([(1, "AAA", _flat(100.0, 5))], {}))
    rand = [(i, f"R{i}", _path(_walk(100 + i, 40, start=50.0 + 10 * i, vol=0.03)))
            for i in range(1, 5)]  # fmt: skip
    run("four random walks with scattered signals over 40 sessions, partially gated",
        _panel_doc(rand, {1: [1, 15], 2: [3, 20], 3: [3, 8, 30], 4: [10]},
                   turnover={1: 3e7, 2: 6e7, 3: 4e7, 4: 8e7}),
        gate=[(j % 9) != 2 for j in range(40)])

    # -- summarise / yearly -----------------------------------------------------------------------
    from baskfy_core.twt.backtest import run_backtest

    golden = _golden()
    results: list[dict[str, Any]] = []
    for inputs in runs:
        stored = json.loads(golden.render(golden._enc(inputs)))
        import numpy as np

        result = run_backtest(_panel(stored["panel"]), np.array(stored["gate_open"], dtype=bool),
                              _params(stored["params"]))  # fmt: skip
        results.append(golden._enc(result))

    def hand(sessions: list[dt.date], equity: list[str], trades: list[dict[str, Any]],
             open_positions: list[int] | None = None) -> dict[str, Any]:  # fmt: skip
        return {"params": _params_doc(), "trades": trades, "sessions": sessions,
                "equity": [D(e) for e in equity],
                "open_positions": open_positions or [0] * len(sessions),
                "skipped": {}, "clamped_below_stop": 0}  # fmt: skip

    def trade(sym: str, exit_on: dt.date, pnl: str, ret: str, reason: str = "STOP_HIT",
              hold: int = 10) -> dict[str, Any]:  # fmt: skip
        return {"symbol": sym, "instrument_id": 1, "entry_date": dt.date(2023, 11, 1),
                "exit_date": exit_on, "entry_price": D("100"), "exit_price": D("110"),
                "quantity": 10, "pnl_inr": D(pnl), "return_pct": D(ret), "r_multiple": None,
                "hold_sessions": hold, "reason": reason, "cap": "SLOT"}  # fmt: skip

    years = [dt.date(2023, 12, 27), dt.date(2023, 12, 28), dt.date(2023, 12, 29),
             dt.date(2024, 1, 1), dt.date(2024, 6, 3), dt.date(2024, 12, 31),
             dt.date(2025, 1, 2), dt.date(2025, 3, 3)]  # fmt: skip
    multi = hand(years, ["1000000", "1010000", "990000", "1005000", "1200000", "1100000",
                         "1150000", "1300000"],
                 [trade("W1", dt.date(2023, 12, 29), "5000", "5.0"),
                  trade("L1", dt.date(2024, 6, 3), "-2000", "-2.0", hold=4),
                  trade("W2", dt.date(2024, 6, 3), "30000", "30.0", "END_OF_RUN", hold=100),
                  trade("Z0", dt.date(2025, 3, 3), "0", "0", "NO_BAR", hold=1)],
                 [1, 1, 0, 1, 2, 2, 1, 0])  # fmt: skip
    leading = hand(years[:5], ["0", "0", "1000000", "1000000", "1100000"], [])
    flat = hand(years[:4], ["1000000"] * 4, [])
    one = hand(years[:1], ["1000000"], [])
    empty = hand([], [], [])
    winners = hand(years[:3], ["1000000", "1050000", "1100000"],
                   [trade("W", dt.date(2023, 12, 29), "100000", "10.0")])  # fmt: skip

    sm = mod + "summarise"
    for i in (0, 1, 2, 3, 14):
        out.append((sm, f"the result of run_backtest case {i + 1:03d}", {"result": results[i]}))
    out.append((sm, "three years by hand: drawdown peak/trough dates, profit factor, by_reason",
                {"result": multi}))  # fmt: skip
    out.append((sm, "leading zero equity is skipped (a window before the first mark)",
                {"result": leading}))  # fmt: skip
    out.append((sm, "a flat curve: sharpe NaN (null), no drawdown, calmar null",
                {"result": flat}))  # fmt: skip
    out.append((sm, "only winners: profit_factor null", {"result": winners}))
    out.append((sm, "one point: None", {"result": one}))
    out.append((sm, "no sessions: None", {"result": empty}))

    yl = mod + "yearly"
    for i in (0, 14):
        out.append((yl, f"the result of run_backtest case {i + 1:03d}", {"result": results[i]}))
    out.append((yl, "three calendar years; each opens on the previous close", {"result": multi}))
    out.append((yl, "leading zeros: the first year opens at zero equity -> 0.0",
                {"result": leading}))  # fmt: skip
    out.append((yl, "a flat year", {"result": flat}))
    out.append((yl, "no sessions: []", {"result": empty}))
    return out, runs


# ---------------------------------------------------------------------------
# The table and the driver
# ---------------------------------------------------------------------------

CALLS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "baskfy_core.twt.calendar.session_counts": call_session_counts,
    "baskfy_core.twt.calendar.thin_sessions": call_thin_sessions,
    "baskfy_core.twt.calendar.build_calendar": call_build_calendar,
    "baskfy_core.twt.calendar.drop_thin_sessions": call_drop_thin_sessions,
    "baskfy_core.twt.calendar.as_shared_config": call_calendar_shared,
    "baskfy_core.twt.indicators.bucket_keys": call_bucket_keys,
    "baskfy_core.twt.indicators.require_columns": call_require_columns,
    "baskfy_core.twt.indicators.with_twt_indicators": call_with_twt_indicators,
    "baskfy_core.twt.signals.weekly_column": call_weekly_column,
    "baskfy_core.twt.signals.weekly_closes": call_weekly_closes,
    "baskfy_core.twt.signals.month_low_back": call_month_low_back,
    "baskfy_core.twt.signals.tight_state": call_tight_state,
    "baskfy_core.twt.signals.entry_events": call_entry_events,
    "baskfy_core.twt.signals.liquidity_floor": call_liquidity_floor,
    "baskfy_core.twt.signals.signal_mask": call_signal_mask,
    "baskfy_core.twt.signals.detect_signals": call_detect_signals,
    "baskfy_core.twt.signals.with_twt_columns": call_with_twt_columns,
    "baskfy_core.twt.breadth.gate_for": call_gate_for,
    "baskfy_core.twt.breadth.breadth_above_dma": call_breadth_above_dma,
    "baskfy_core.twt.breadth.breadth_series": call_breadth_series,
    "baskfy_core.twt.breadth.as_shared_config": call_breadth_shared,
    "baskfy_core.twt.sizing.size_entry": call_size_entry,
    "baskfy_core.twt.sizing.first_live_multiplier": call_first_live_multiplier,
    "baskfy_core.twt.exits.tick": call_tick,
    "baskfy_core.twt.exits.tick_floor": call_tick_floor,
    "baskfy_core.twt.exits.initial_stop": call_initial_stop,
    "baskfy_core.twt.exits.stop_is_below_entry": call_stop_is_below_entry,
    "baskfy_core.twt.exits.trail_from": call_trail_from,
    "baskfy_core.twt.exits.clamp_under_close": call_clamp_under_close,
    "baskfy_core.twt.exits.ratchet": call_ratchet,
    "baskfy_core.twt.exits.on_adjustment": call_on_adjustment,
    "baskfy_core.twt.exits.stop_fill": call_stop_fill,
    "baskfy_core.twt.exits.fill_day_stop": call_fill_day_stop,
    "baskfy_core.twt.exits.manage": call_manage,
    "baskfy_core.twt.exits.r_multiple": call_r_multiple,
    "baskfy_core.twt.sleeve.sleeve_value": call_sleeve_value,
    "baskfy_core.twt.plan.client_id_for": call_client_id_for,
    "baskfy_core.twt.plan.stop_for": call_stop_for,
    "baskfy_core.twt.plan.gtt_limit_price": call_gtt_limit_price,
    "baskfy_core.twt.plan.build_entries": call_build_entries,
    "baskfy_core.twt.plan.exit_lines": call_exit_lines,
    "baskfy_core.twt.plan.assemble": call_assemble,
    "baskfy_core.twt.drift.compare": call_compare,
    "baskfy_core.twt.published.PUBLISHED": call_published,
    "baskfy_core.twt.backtest.panel_from_frame": call_panel_from_frame,
    "baskfy_core.twt.backtest.gate_vector": call_gate_vector,
    "baskfy_core.twt.backtest.run_backtest": call_run_backtest,
    "baskfy_core.twt.backtest.summarise": call_summarise,
    "baskfy_core.twt.backtest.yearly": call_yearly,
}

#: Functions whose output frame is sorted before it is written (module docstring).
SORTED_OUTPUT: frozenset[str] = frozenset(
    {"baskfy_core.twt.signals.weekly_closes", "baskfy_core.twt.signals.month_low_back"}
)


def cases() -> list[tuple[str, str, str, dict[str, Any]]]:
    """``(fn, case file stem, why, JSON inputs)`` for every golden, in a fixed order."""
    out: list[tuple[str, str, str, dict[str, Any]]] = []

    def add(fn: str, why: str, inputs: dict[str, Any]) -> None:
        assert fn in CALLS, fn
        short = fn.removeprefix("baskfy_core.twt.")
        n = sum(1 for f, *_ in out if f == fn) + 1
        out.append((fn, f"{short}.case_{n:03d}", why, inputs))

    backtest, _runs = _backtest_cases()
    for block in (
        _calendar_cases(),
        _indicator_cases(),
        _signal_cases(),
        _breadth_cases(),
        _sizing_cases(),
        _exit_cases(),
        _sleeve_cases(),
        _plan_cases(),
        _drift_cases(),
        backtest,
    ):
        for fn, why, inputs in block:
            add(fn, why, inputs)
    return out


def recompute(doc: dict[str, Any]) -> Any:
    """Run the golden's function on its stored inputs (for a drift test to compare)."""
    return CALLS[doc["fn"]](doc["inputs"])


def render_case(fn: str, why: str, inputs: dict[str, Any]) -> str:
    golden = _golden()
    # Round-trip the inputs through their JSON text first, so the stored output is the output of
    # exactly what the file says the inputs are.
    stored = json.loads(golden.render(golden._enc(inputs)))
    notes = f"{why}. {SORTED_NOTE}. {NOTES}" if fn in SORTED_OUTPUT else f"{why}. {NOTES}"
    doc = golden.document(fn=fn, inputs=stored, output=CALLS[fn](stored), notes=notes, stable=True)
    return golden.render(doc)


def dump_twt() -> list[Path]:
    golden = _golden()
    directory = golden.GOLDEN_DIR / LANE
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for fn, stem, why, inputs in cases():
        path = directory / f"{stem}.json"
        path.write_text(render_case(fn, why, inputs), encoding="utf-8")
        written.append(path)
    return written


if __name__ == "__main__":
    paths = dump_twt()
    print(f"{len(paths)} twt goldens written under go/testdata/golden/{LANE}/")
