"""Canonical JSON dumper for Go parity goldens (docs/go-rewrite/03-parity-and-gates.md).

Import from a lane's dumper, run with the tree's own venv:

    from tools.parity.golden import dump
    dump("L1/score/case_001", fn="baskfy_core.score.score_frame",
         inputs={"frame": df, "asof": date(2026, 8, 22)}, output=score_frame(df, asof),
         known_bug=None, tolerance={"float": 1e-9})

Writes go/testdata/golden/<case>.json. Never modifies Python, never touches the network.
Encoding rules (mirrored by go/internal/testkit/golden.go):
  Decimal -> string ("12.34")        date/datetime -> ISO 8601 (datetimes carry the offset
                                     when they have one; a naive datetime is exchange-local)
  NaN/inf -> null                    set/frozenset -> sorted list
  pandas.DataFrame -> {"columns": [...], "dtypes": {...}, "index": [...], "rows": [[...], ...]}
  pandas.Series    -> {"index": [...], "values": [...]}
  polars.DataFrame -> {"columns": [...], "dtypes": {polars names}, "rows": [[...], ...]}
                      encoded natively (never via pandas): a Date column stays a date, an
                      Int32 stays an int, a null stays null; column order is the frame's
  polars.Series    -> {"dtype": ..., "values": [...]}
  numpy scalars/arrays -> python scalars/lists       dataclass/pydantic -> dict
  StrEnum -> its value
Key order is sorted; floats are written with repr precision so 1e-9 comparisons are meaningful.

Lanes that live in this file
----------------------------
``swing`` (SW12, docs/swing/STANDING-ANSWERS.md B11): ``python golden.py swing`` dumps the six
pure swing functions over a fixed fixture set into ``go/testdata/golden/L1/swing/``. Those
goldens are written with ``stable=True`` — no timestamp, interpreter or cwd in ``meta`` — so
two runs are byte-identical and ``packages/core/tests/test_swing_goldens.py`` can recompute
each one from its stored inputs and compare the file byte for byte.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import math
import os
import sys
from collections.abc import Callable, Iterator, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = ROOT / "go" / "testdata" / "golden"
JSON_KWARGS: dict[str, Any] = {"indent": 1, "sort_keys": True, "ensure_ascii": False}


def _enc(o: Any) -> Any:  # noqa: PLR0911 - one branch per type is the point
    if o is None or isinstance(o, (bool, int, str)):
        return o
    if isinstance(o, float):
        return None if (math.isnan(o) or math.isinf(o)) else o
    if isinstance(o, Decimal):
        return None if o.is_nan() else format(o, "f")
    if isinstance(o, _dt.datetime):
        return o.isoformat()
    if isinstance(o, _dt.date):
        return o.isoformat()
    if isinstance(o, (set, frozenset)):
        return sorted((_enc(x) for x in o), key=json.dumps)
    if isinstance(o, dict):
        return {str(k): _enc(v) for k, v in sorted(o.items(), key=lambda kv: str(kv[0]))}
    if isinstance(o, (list, tuple)):
        return [_enc(x) for x in o]
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return _enc(dataclasses.asdict(o))
    if hasattr(o, "model_dump"):  # pydantic v2
        return _enc(o.model_dump())
    try:  # numpy without importing numpy
        import numpy as np  # type: ignore[import-not-found]

        if isinstance(o, np.generic):
            return _enc(o.item())
        if isinstance(o, np.ndarray):
            return _enc(o.tolist())
    except ImportError:
        pass
    try:  # polars before pandas: a polars frame is encoded natively, never through to_pandas()
        import polars as pl  # type: ignore[import-not-found]

        if isinstance(o, pl.DataFrame):
            return {
                "columns": list(o.columns),
                "dtypes": {c: str(t) for c, t in o.schema.items()},
                "rows": [_enc(list(r)) for r in o.rows()],
            }
        if isinstance(o, pl.Series):
            return {"dtype": str(o.dtype), "values": _enc(o.to_list())}
    except ImportError:
        pass
    try:
        import pandas as pd  # type: ignore[import-not-found]

        if isinstance(o, pd.DataFrame):
            return {
                "columns": [str(c) for c in o.columns],
                "dtypes": {str(c): str(t) for c, t in o.dtypes.items()},
                "index": _enc(list(o.index)),
                "rows": [_enc(list(r)) for r in o.itertuples(index=False, name=None)],
            }
        if isinstance(o, pd.Series):
            return {"index": _enc(list(o.index)), "values": _enc(list(o.values))}
        if isinstance(o, pd.Timestamp):
            return o.isoformat()
    except ImportError:
        pass
    if hasattr(o, "__dict__"):
        return _enc(vars(o))
    raise TypeError(f"golden: cannot encode {type(o).__name__}")


def document(  # noqa: PLR0913 - one keyword per field of the golden
    *,
    fn: str,
    inputs: Any,
    output: Any,
    known_bug: str | None = None,
    tolerance: dict[str, float] | None = None,
    notes: str | None = None,
    stable: bool = False,
) -> dict[str, Any]:
    """The golden as a JSON-ready dict. ``stable=True`` leaves out the timestamp, the
    interpreter and the cwd, so the same inputs always produce the same bytes."""
    meta: dict[str, Any] = {
        "known_bug": known_bug,
        "tolerance": tolerance or {"float": 1e-9},
        "notes": notes,
    }
    if stable:
        meta["stable"] = True
    else:
        meta["dumped_at"] = _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")
        meta["python"] = sys.version.split()[0]
        meta["cwd"] = os.path.relpath(os.getcwd(), ROOT)
    return {"fn": fn, "inputs": _enc(inputs), "output": _enc(output), "meta": meta}


def render(doc: dict[str, Any]) -> str:
    """The exact text a golden file holds — one serialisation, shared with the drift test."""
    return json.dumps(doc, **JSON_KWARGS) + "\n"


def dump(  # noqa: PLR0913 - one keyword per field of the golden
    case: str,
    *,
    fn: str,
    inputs: Any,
    output: Any,
    known_bug: str | None = None,
    tolerance: dict[str, float] | None = None,
    notes: str | None = None,
    stable: bool = False,
) -> Path:
    """Write one golden case; returns its path. `case` is '<lane>/<module>/<name>'."""
    path = GOLDEN_DIR / f"{case}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = document(
        fn=fn,
        inputs=inputs,
        output=output,
        known_bug=known_bug,
        tolerance=tolerance,
        notes=notes,
        stable=stable,
    )
    path.write_text(render(doc), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Lane: swing — the six pure functions of baskfy_core.swing (STANDING-ANSWERS B11)
# ---------------------------------------------------------------------------
#
# Layout: ``L1/swing/<function>.case_NNN.json`` — lane / module / case, the shape docs/go-rewrite/03
# names, with the module being ``swing`` and the case naming its function. Flat on purpose: one
# directory the Go lane globs, and one directory a shell can checksum.
#
# Conventions the Go lane must not misread (they are restated in every case's ``meta.notes``):
#   * ``detect_setups`` inputs are the **raw bars as the worker hands them** — the adjusted OHLC
#     series (house rule 6: ``close`` is adjusted; ``adj_factor`` absent means 1.0) — plus the
#     ``as_of`` date and the full ``SwingConfig``. The function under test is
#     ``detect_setups(with_swing_indicators(bars, config), as_of, config)``; its ``trigger`` and
#     ``stop_ref`` are adjusted prices, ``adj_factor`` rides along.
#   * Every ``Decimal`` is a string, every ``date`` is ``YYYY-MM-DD``, a naive ``datetime`` is
#     exchange-local IST with no offset. Percentages are percents (``3.5`` is 3.5%).
#   * ``build_entries`` returns a tuple; its golden output is
#     ``{"lines": [...], "skipped": [...]}``.
#   * A polars frame is ``{"columns", "dtypes", "rows"}`` with the frame's own column order.

SWING_LANE = "L1/swing"
SWING_FIXTURES_DIR = ROOT / "decile-blueprint" / "packages" / "core" / "tests"
SWING_NOTES = (
    "Prices are the adjusted series (house rule 6); Decimals are strings; dates are ISO days; "
    "a naive datetime is exchange-local IST. Fixtures: packages/core/tests/swing_fixtures.py and "
    "the swing test modules. Regenerate with `python tools/parity/golden.py swing` from "
    "decile-blueprint under `uv run`; packages/core/tests/test_swing_goldens.py recomputes "
    "every case from these stored inputs."
)


@dataclasses.dataclass(frozen=True)
class SwingCase:
    fn: str
    name: str
    inputs: dict[str, Any]
    notes: str


def _tupled(value: Any) -> Any:
    return tuple(_tupled(v) for v in value) if isinstance(value, list) else value


def _decimal(value: Any) -> Any:
    return None if value is None else Decimal(str(value))


def _date(value: str) -> _dt.date:
    return _dt.date.fromisoformat(value)


def swing_config_from(doc: dict[str, Any]) -> Any:
    """A ``SwingConfig`` from its encoded dict (tuples come back from lists)."""
    from baskfy_core.swing import config as cfg

    parts = {
        "liquidity": cfg.LiquidityConfig,
        "flag": cfg.FlagConfig,
        "ep": cfg.EpConfig,
        "parabolic": cfg.ParabolicConfig,
        "sizing": cfg.SizingConfig,
        "stops": cfg.StopConfig,
        "watch": cfg.WatchConfig,
        "opening_range": cfg.OpeningRangeConfig,
        "market": cfg.MarketConfig,
    }
    fields: dict[str, Any] = {}
    for key, value in doc.items():
        if key in parts:
            fields[key] = parts[key](**{k: _tupled(v) for k, v in value.items()})
        else:
            fields[key] = _tupled(value)
    return cfg.SwingConfig(**fields)


def _sub_config(cls: Any, doc: dict[str, Any]) -> Any:
    return cls(**{k: _tupled(v) for k, v in doc.items()})


def polars_frame_from(doc: dict[str, Any]) -> Any:
    """A polars frame from ``{"columns", "dtypes", "rows"}`` with the dtypes it was dumped with."""
    import polars as pl

    dtypes = {
        "Float64": pl.Float64,
        "Int64": pl.Int64,
        "Int32": pl.Int32,
        "Boolean": pl.Boolean,
        "String": pl.String,
        "Date": pl.Date,
    }
    columns: list[str] = doc["columns"]
    schema = {c: dtypes[doc["dtypes"][c]] for c in columns}
    rows = [
        [
            _date(v) if (schema[c] is pl.Date and v is not None) else v
            for c, v in zip(columns, row, strict=True)
        ]
        for row in doc["rows"]
    ]
    return pl.DataFrame(rows, schema=schema, orient="row")


# -- decoders: JSON inputs -> the keyword arguments each function takes -----------------------


def _decode_detect_setups(inputs: dict[str, Any]) -> dict[str, Any]:
    return {
        "bars": polars_frame_from(inputs["bars"]),
        "as_of": _date(inputs["as_of"]),
        "config": swing_config_from(inputs["config"]),
    }


def _decode_size_position(inputs: dict[str, Any]) -> dict[str, Any]:
    from baskfy_core.swing.config import SizingConfig

    return {
        "equity": _decimal(inputs["equity"]),
        "cash_available": _decimal(inputs["cash_available"]),
        "entry": _decimal(inputs["entry"]),
        "stop": _decimal(inputs["stop"]),
        "avg_turnover_inr": _decimal(inputs["avg_turnover_inr"]),
        "config": _sub_config(SizingConfig, inputs["config"]),
        "max_stop_distance_pct": _decimal(inputs["max_stop_distance_pct"]),
    }


def _decode_manage(inputs: dict[str, Any]) -> dict[str, Any]:
    from baskfy_core.swing.config import StopConfig
    from baskfy_core.swing.stops import DailyBar, OpenPosition, TrailMa

    p = inputs["position"]
    b = inputs["bar"]
    return {
        "position": OpenPosition(
            symbol=p["symbol"],
            entry_date=_date(p["entry_date"]),
            entry=_decimal(p["entry"]),
            initial_stop=_decimal(p["initial_stop"]),
            stop=_decimal(p["stop"]),
            quantity=p["quantity"],
            partial_done=p["partial_done"],
            trail=TrailMa(p["trail"]),
            is_ep_gap_day=p["is_ep_gap_day"],
        ),
        "bar": DailyBar(
            date=_date(b["date"]),
            open=_decimal(b["open"]),
            high=_decimal(b["high"]),
            low=_decimal(b["low"]),
            close=_decimal(b["close"]),
            ma10=_decimal(b["ma10"]),
            ma20=_decimal(b["ma20"]),
            bars_since_entry=b["bars_since_entry"],
        ),
        "config": _sub_config(StopConfig, inputs["config"]),
    }


def _decode_exposure_tier(inputs: dict[str, Any]) -> dict[str, Any]:
    from baskfy_core.swing.config import MarketConfig
    from baskfy_core.swing.market import MarketGate

    return {
        "current_level": inputs["current_level"],
        "closed_r_multiples": [Decimal(r) for r in inputs["closed_r_multiples"]],
        "gate": MarketGate(inputs["gate"]),
        "config": _sub_config(MarketConfig, inputs["config"]),
        "drawdown_pct": inputs["drawdown_pct"],
        "was_drawdown_locked": inputs["was_drawdown_locked"],
    }


def _decode_build_entries(inputs: dict[str, Any]) -> dict[str, Any]:
    from baskfy_core.swing.config import Setup
    from baskfy_core.swing.market import ExposureTier, MarketGate
    from baskfy_core.swing.plan import SwingAccount, WatchItem

    a = inputs["account"]
    t = inputs["tier"]
    return {
        "as_of": _date(inputs["as_of"]),
        "watch": [
            WatchItem(
                symbol=w["symbol"],
                setup=Setup(w["setup"]),
                trigger=_decimal(w["trigger"]),
                stop_ref=_decimal(w["stop_ref"]),
                adr_pct=_decimal(w["adr_pct"]),
                avg_turnover_inr=_decimal(w["avg_turnover_inr"]),
                score=_decimal(w["score"]),
                locked_upper_circuit=w["locked_upper_circuit"],
            )
            for w in inputs["watch"]
        ],
        "account": SwingAccount(
            equity=_decimal(a["equity"]),
            cash_available=_decimal(a["cash_available"]),
            open_symbols=frozenset(a["open_symbols"]),
            open_exposure_inr=_decimal(a["open_exposure_inr"]),
        ),
        "gate": MarketGate(inputs["gate"]),
        "tier": ExposureTier(
            level=t["level"],
            max_open_positions=t["max_open_positions"],
            max_exposure_pct=t["max_exposure_pct"],
            new_entries_allowed=t["new_entries_allowed"],
            drawdown_locked=t["drawdown_locked"],
        ),
        "config": swing_config_from(inputs["config"]),
        "entries_already_today": inputs["entries_already_today"],
        "risk_multiplier": _decimal(inputs["risk_multiplier"]),
    }


def _decode_evaluate_trigger(inputs: dict[str, Any]) -> dict[str, Any]:
    from baskfy_core.swing.config import OpeningRangeConfig
    from baskfy_core.swing.opening_range import OpeningRange

    o = inputs["opening"]
    return {
        "last_price": _decimal(inputs["last_price"]),
        "opening": OpeningRange(
            high=_decimal(o["high"]),
            low=_decimal(o["low"]),
            window_minutes=o["window_minutes"],
            complete=o["complete"],
            candles=o["candles"],
        ),
        "pivot_high": _decimal(inputs["pivot_high"]),
        "low_of_day": _decimal(inputs["low_of_day"]),
        "upper_circuit": _decimal(inputs["upper_circuit"]),
        "at": _dt.datetime.fromisoformat(inputs["at"]),
        "config": _sub_config(OpeningRangeConfig, inputs["config"]),
    }


# -- callers: keyword arguments -> the function's output, in the shape the golden stores --------


def _call_detect_setups(**kw: Any) -> Any:
    from baskfy_core.swing.indicators import with_swing_indicators
    from baskfy_core.swing.setups import detect_setups

    return detect_setups(with_swing_indicators(kw["bars"], kw["config"]), kw["as_of"], kw["config"])


def _call_size_position(**kw: Any) -> Any:
    from baskfy_core.swing.sizing import size_position

    return size_position(**kw)


def _call_manage(**kw: Any) -> Any:
    from baskfy_core.swing.stops import manage

    return manage(kw["position"], kw["bar"], kw["config"])


def _call_exposure_tier(**kw: Any) -> Any:
    from baskfy_core.swing.market import exposure_tier

    return exposure_tier(**kw)


def _call_build_entries(**kw: Any) -> Any:
    from baskfy_core.swing.plan import build_entries

    lines, skipped = build_entries(**kw)
    return {"lines": lines, "skipped": skipped}


def _call_evaluate_trigger(**kw: Any) -> Any:
    from baskfy_core.swing.opening_range import evaluate_trigger

    return evaluate_trigger(**kw)


Decoder = Callable[[dict[str, Any]], dict[str, Any]]
Caller = Callable[..., Any]

#: fn name -> (decode stored inputs, run the live function). The drift test walks this table.
SWING_FUNCTIONS: dict[str, tuple[Decoder, Caller]] = {
    "baskfy_core.swing.setups.detect_setups": (_decode_detect_setups, _call_detect_setups),
    "baskfy_core.swing.sizing.size_position": (_decode_size_position, _call_size_position),
    "baskfy_core.swing.stops.manage": (_decode_manage, _call_manage),
    "baskfy_core.swing.market.exposure_tier": (_decode_exposure_tier, _call_exposure_tier),
    "baskfy_core.swing.plan.build_entries": (_decode_build_entries, _call_build_entries),
    "baskfy_core.swing.opening_range.evaluate_trigger": (
        _decode_evaluate_trigger,
        _call_evaluate_trigger,
    ),
}


def swing_recompute(doc: dict[str, Any]) -> Any:
    """Run the golden's function on its stored inputs; the drift test compares this to the file."""
    decode, call = SWING_FUNCTIONS[doc["fn"]]
    return call(**decode(doc["inputs"]))


def swing_case_path(fn: str, name: str) -> Path:
    return GOLDEN_DIR / SWING_LANE / f"{fn.rsplit('.', 1)[1]}.{name}.json"


# -- the fixture set --------------------------------------------------------------------------


def _swing_cases() -> Iterator[SwingCase]:  # noqa: PLR0915 - one block per function, in order
    """Every case, in a fixed order. Inputs are Python objects; the dumper encodes them."""
    if str(SWING_FIXTURES_DIR) not in sys.path:
        sys.path.insert(0, str(SWING_FIXTURES_DIR))
    import swing_fixtures as fx

    from baskfy_core.swing.config import (
        DEFAULT_SWING_CONFIG,
        EpConfig,
        MarketConfig,
        OpeningRangeConfig,
        Setup,
        SizingConfig,
        StopConfig,
    )
    from baskfy_core.swing.market import ExposureTier, MarketGate
    from baskfy_core.swing.opening_range import OpeningRange
    from baskfy_core.swing.plan import SwingAccount, WatchItem
    from baskfy_core.swing.stops import DailyBar, OpenPosition, TrailMa

    D = Decimal
    as_of = fx.last_date()

    # -- detect_setups: the shapes docs/swing/04 §2-§4 describe, drawn by swing_fixtures --------
    def detect(name: str, note: str, bars: Any, config: Any = DEFAULT_SWING_CONFIG) -> SwingCase:
        return SwingCase(
            "baskfy_core.swing.setups.detect_setups",
            name,
            {"bars": fx.frame(*bars), "as_of": as_of, "config": config},
            f"{note}. detect_setups(with_swing_indicators(bars, config), as_of, config); bars are "
            "the raw adjusted OHLCV rows the worker hands the detectors (adj_factor absent = 1.0).",
        )

    breakout_rows = fx.flag_series()
    prior_pivot = max(float(str(r["high"])) for r in breakout_rows[-21:-1])
    breakout_close = prior_pivot * 1.03
    breakout_rows[-1] = {
        **breakout_rows[-1],
        "open": prior_pivot * 0.99,
        "high": breakout_close * 1.01,
        "low": prior_pivot * 0.98,
        "close": breakout_close,
        "volume": 5e6,
    }
    loose_ep = dataclasses.replace(
        DEFAULT_SWING_CONFIG, ep=dataclasses.replace(EpConfig(), min_gap_pct=5.0)
    )
    yield detect(
        "case_001",
        "textbook flag SETTING_UP beside a flat name",
        [fx.flag_series(), fx.flat_series()],
    )
    yield detect(
        "case_002", "BREAKOUT_TODAY: close above yesterday's pivot on 5x volume", [breakout_rows]
    )
    yield detect(
        "case_003",
        "EP GAP_DAY out of a neglected base, beside a flat name",
        [fx.ep_series(), fx.flat_series()],
    )
    yield detect(
        "case_004",
        "EP locked at the upper band: detected, locked_upper_circuit true",
        [fx.ep_series(locked=True)],
    )
    yield detect("case_005", "PARABOLIC_SHORT RUNNING after six 12% days", [fx.parabolic_series()])
    yield detect(
        "case_006",
        "PARABOLIC_SHORT EXHAUSTION on the first red close",
        [fx.parabolic_series(red_last_day=True)],
    )
    yield detect(
        "case_007",
        "the mixed universe: two flags (one noisy), an EP, a runner, a flat name; ranked within "
        "setup",
        [
            fx.flag_series(1, "FLAG1"),
            fx.flag_series(5, "FLAG2", base_noise=3.0, seed=7),
            fx.ep_series(),
            fx.parabolic_series(),
            fx.flat_series(),
        ],
    )
    yield detect(
        "case_008",
        "a 6% gap is not an EP under the default 10%: the empty candidate frame",
        [fx.ep_series(gap=0.06)],
    )
    yield detect(
        "case_009",
        "the same 6% gap under min_gap_pct=5.0: the config is read, not a literal",
        [fx.ep_series(gap=0.06)],
        loose_ep,
    )
    yield detect(
        "case_010",
        "a 15% pole is not a flagpole; a 3% daily gain is not parabolic: empty",
        [fx.flag_series(pole_gain=0.15), fx.parabolic_series(daily_gain=0.03)],
    )

    # -- size_position: docs/swing/04 §5, the four caps and the four refusals -------------------
    def size(name: str, note: str, **over: Any) -> SwingCase:
        inputs: dict[str, Any] = {
            "equity": D("1000000"),
            "cash_available": D("1000000"),
            "entry": D("500"),
            "stop": D("480"),
            "avg_turnover_inr": D("500000000"),
            "config": SizingConfig(),
            "max_stop_distance_pct": D("10"),
        }
        inputs.update(over)
        return SwingCase("baskfy_core.swing.sizing.size_position", name, inputs, note)

    yield size(
        "case_001",
        "the worked example: stop 4% below, 0.5% risk, 12.5% of the account, cap RISK",
        entry=D("100"),
        stop=D("96"),
    )
    yield size(
        "case_002", "a 1% stop is capped by the 20% position limit", entry=D("100"), stop=D("99")
    )
    yield size("case_003", "cash on hand binds before equity", cash_available=D("50000"))
    yield size(
        "case_004",
        "the turnover cap: 1% of a ₹20 lakh average day",
        avg_turnover_inr=D("2000000"),
        entry=D("100"),
        stop=D("96"),
    )
    yield size("case_005", "stop at the entry: STOP_NOT_BELOW_ENTRY", stop=D("500"))
    yield size(
        "case_006", "a 15% stop is refused STOP_TOO_WIDE, not shrunk", entry=D("100"), stop=D("85")
    )
    yield size(
        "case_007",
        "0.01% risk sizes below the minimum trade value",
        config=dataclasses.replace(SizingConfig(), risk_per_trade_pct=0.01),
        entry=D("100"),
        stop=D("96"),
    )
    yield size("case_008", "no equity is NO_EQUITY", equity=D("0"))
    yield size(
        "case_009",
        "no turnover reading: the turnover cap is not applied",
        avg_turnover_inr=None,
        entry=D("100"),
        stop=D("96"),
    )
    yield size(
        "case_010",
        "a stop exactly at the widest allowed distance (ADR 4.5%) is accepted",
        entry=D("200"),
        stop=D("191"),
        max_stop_distance_pct=D("4.50"),
    )
    yield size(
        "case_011",
        "half risk (A9, the first live sessions): the multiplier applied to the config",
        config=dataclasses.replace(SizingConfig(), risk_per_trade_pct=0.25),
        entry=D("100"),
        stop=D("96"),
    )

    # -- manage: docs/swing/04 §6, one bar at a time ----------------------------------------------
    base_position = OpenPosition(
        symbol="X",
        entry_date=_dt.date(2026, 9, 1),
        entry=D("100"),
        initial_stop=D("95"),
        stop=D("95"),
        quantity=300,
        partial_done=False,
        trail=TrailMa.MA10,
        is_ep_gap_day=False,
    )

    def bar(  # noqa: PLR0913 - one keyword per bar field a rule reads
        close: str,
        *,
        low: str | None = None,
        ma10: str = "90",
        ma20: str = "88",
        day: int = 1,
        open_: str | None = None,
    ) -> DailyBar:
        c = D(close)
        return DailyBar(
            date=_dt.date(2026, 9, 1) + _dt.timedelta(days=day),
            open=D(open_) if open_ else c,
            high=c * D("1.01"),
            low=D(low) if low else c * D("0.99"),
            close=c,
            ma10=D(ma10),
            ma20=D(ma20),
            bars_since_entry=day,
        )

    def managed(
        name: str, note: str, b: DailyBar, config: StopConfig | None = None, **over: Any
    ) -> SwingCase:
        return SwingCase(
            "baskfy_core.swing.stops.manage",
            name,
            {
                "position": dataclasses.replace(base_position, **over),
                "bar": b,
                "config": config or StopConfig(),
            },
            note,
        )

    yield managed(
        "case_001",
        "the hard stop hit inside the bar: STOPPED_OUT for the whole position",
        bar("97", low="94.5"),
    )
    yield managed(
        "case_002",
        "an EP that closes red on its gap day is sold",
        bar("98", open_="101", day=0),
        is_ep_gap_day=True,
    )
    yield managed(
        "case_003",
        "a close below the 20-day trail sells the remainder",
        bar("102", ma20="103", day=8),
        trail=TrailMa.MA20,
    )
    yield managed(
        "case_004", "the trail is not consulted on the entry day", bar("102", ma10="103", day=0)
    )
    yield managed(
        "case_005", "day 3, green: a third sold and the stop to breakeven", bar("108", day=3)
    )
    yield managed("case_006", "day 4, red: no partial, nothing to do", bar("99", day=4))
    yield managed(
        "case_007", "day 2, 1.6R showing: breakeven at R, no partial yet", bar("108", day=2)
    )
    yield managed(
        "case_008",
        "day 6: outside the partial window, the stop already at breakeven: HOLD",
        bar("108", day=6),
        stop=D("100"),
    )
    yield managed(
        "case_009",
        "partial already done on day 4: no second partial",
        bar("108", day=4),
        partial_done=True,
        stop=D("100"),
    )
    yield managed("case_010", "1R showing on day 1: the stop rises to the entry", bar("105", day=1))
    yield managed(
        "case_011",
        "a stop already above the entry is never lowered",
        bar("105", day=1),
        stop=D("104"),
    )
    yield managed("case_012", "nothing to do is said explicitly", bar("101", day=1))
    yield managed("case_013", "day 5 is the last day of the partial window", bar("103", day=5))
    yield managed(
        "case_014",
        "a tiny position: the partial rounds to one share, never the whole",
        bar("108", day=3),
        quantity=4,
    )
    yield managed("case_015", "a bar exactly at the stop is a hit", bar("97", low="95"))
    yield managed(
        "case_016", "a close exactly on the trail MA is not below it", bar("103", ma10="103", day=8)
    )

    # -- exposure_tier: docs/swing/04 §8, the ladder -------------------------------------------
    def tier(
        name: str,
        note: str,
        level: int,
        rs: Sequence[str],
        gate: MarketGate = MarketGate.GREEN,
        **over: Any,
    ) -> SwingCase:
        inputs: dict[str, Any] = {
            "current_level": level,
            "closed_r_multiples": [D(r) for r in rs],
            "gate": gate,
            "config": MarketConfig(),
            "drawdown_pct": 0.0,
            "was_drawdown_locked": False,
        }
        inputs.update(over)
        return SwingCase("baskfy_core.swing.market.exposure_tier", name, inputs, note)

    yield tier(
        "case_001",
        "RED drops to rung 0 and forbids entries whatever the results",
        3,
        ["2", "2", "2", "2", "2"],
        MarketGate.RED,
    )
    yield tier(
        "case_002",
        "five closed trades net positive in a GREEN tape: one rung up",
        0,
        ["1", "-1", "3", "-1", "2"],
    )
    yield tier("case_003", "already at the top rung: stays", 3, ["1", "1", "1", "1", "1"])
    yield tier(
        "case_004",
        "eight big winners still climb exactly one rung",
        0,
        ["5", "5", "5", "5", "5", "5", "5", "5"],
    )
    yield tier(
        "case_005",
        "AMBER holds the rung even with good results",
        1,
        ["1", "1", "1", "1", "1"],
        MarketGate.AMBER,
    )
    yield tier(
        "case_006", "three straight losses step one rung down", 2, ["3", "2", "-1", "-1", "-1"]
    )
    yield tier("case_007", "three losses at rung 0 stay at rung 0", 0, ["-1", "-1", "-1"])
    yield tier("case_008", "two closed trades cannot climb", 0, ["2", "2"])
    yield tier("case_009", "no history at rung 2: the rung's own limits", 2, [])
    yield tier("case_010", "a 15% drawdown locks the sleeve at rung 0", 3, [], drawdown_pct=15.0)
    yield tier(
        "case_011",
        "locked yesterday, 12% today: still locked (hysteresis)",
        3,
        [],
        drawdown_pct=12.0,
        was_drawdown_locked=True,
    )
    yield tier(
        "case_012",
        "locked yesterday, 8% today: recovered, resumes at the bottom rung",
        3,
        [],
        drawdown_pct=8.0,
        was_drawdown_locked=True,
    )
    yield tier("case_013", "a rung above the ladder is clamped to the top", 9, [])
    yield tier(
        "case_014",
        "net R exactly zero over five trades does not climb",
        1,
        ["1", "1", "-1", "-1", "0"],
    )

    # -- build_entries: docs/swing/04 §9, the plan's skip order ---------------------------------
    plan_as_of = _dt.date(2026, 9, 2)
    plan_tier = ExposureTier(1, 4, 50.0, new_entries_allowed=True)
    plan_account = SwingAccount(D("1000000"), D("1000000"), frozenset(), D("0"))

    def item(  # noqa: PLR0913 - one keyword per watch field
        symbol: str,
        *,
        score: str = "70",
        setup: Setup = Setup.FLAG,
        trigger: str = "100",
        stop: str | None = "96",
        locked: bool = False,
    ) -> WatchItem:
        return WatchItem(
            symbol,
            setup,
            D(trigger),
            None if stop is None else D(stop),
            D("5"),
            D("300000000"),
            D(score),
            locked,
        )

    def plan(name: str, note: str, watch: Sequence[WatchItem], **over: Any) -> SwingCase:
        inputs: dict[str, Any] = {
            "as_of": plan_as_of,
            "watch": list(watch),
            "account": plan_account,
            "gate": MarketGate.GREEN,
            "tier": plan_tier,
            "config": DEFAULT_SWING_CONFIG,
            "entries_already_today": 0,
            "risk_multiplier": D(1),
        }
        inputs.update(over)
        return SwingCase("baskfy_core.swing.plan.build_entries", name, inputs, note)

    yield plan(
        "case_001", "one flag: trigger, stop, 1250 shares, ₹5,000 at risk, trail MA20", [item("A")]
    )
    yield plan(
        "case_002",
        "best score first and three per session: HIGH, MID, X lined; Y and LOW SESSION_CAP",
        [
            item("LOW", score="40"),
            item("HIGH", score="90"),
            item("MID", score="60"),
            item("X", score="50"),
            item("Y", score="45"),
        ],
    )
    yield plan(
        "case_003",
        "rung 0 allows two: the third is TIER_FULL",
        [item("A", score="90"), item("B", score="80"), item("C")],
        tier=ExposureTier(0, 2, 25.0, new_entries_allowed=True),
    )
    yield plan(
        "case_004",
        "the trader's own max_open_positions=1 binds below the rung",
        [item("A", score="90"), item("B")],
        config=dataclasses.replace(
            DEFAULT_SWING_CONFIG, sizing=dataclasses.replace(SizingConfig(), max_open_positions=1)
        ),
    )
    yield plan(
        "case_005",
        "a 6% stop on a 5% ADR name is SIZE_REFUSED STOP_TOO_WIDE; 4.5% is lined",
        [item("WIDE", stop="94"), item("OK", stop="95.5", score="60")],
    )
    yield plan(
        "case_006",
        "a drawdown-locked sleeve refuses every entry by name",
        [item("A"), item("B")],
        tier=ExposureTier(0, 2, 25.0, new_entries_allowed=False, drawdown_locked=True),
    )
    yield plan(
        "case_007",
        "three open positions count against a rung of four",
        [item("A"), item("B")],
        account=SwingAccount(
            D("1000000"), D("1000000"), frozenset({"P1", "P2", "P3"}), D("300000")
        ),
    )
    yield plan(
        "case_008",
        "a RED gate refuses every entry by name",
        [item("A"), item("B")],
        gate=MarketGate.RED,
    )
    yield plan(
        "case_009",
        "a PARABOLIC_SHORT is never a line",
        [item("RUNNER", setup=Setup.PARABOLIC_SHORT)],
    )
    yield plan(
        "case_010",
        "a held name and a locked name are skipped",
        [item("HELD"), item("LOCKED", locked=True, score="60")],
        account=SwingAccount(D("1000000"), D("1000000"), frozenset({"HELD"}), D("100000")),
    )
    yield plan(
        "case_011",
        "a live gap with no stop yet (A7): a PENDING_RANGE line that reserves a slot",
        [
            item("GAP", setup=Setup.EP, stop=None, trigger="120", score="80"),
            item("A"),
            item("B", score="65"),
            item("C", score="60"),
        ],
    )
    yield plan(
        "case_012",
        "half risk for the first live sessions (A9): 625 shares, not 1250",
        [item("A")],
        risk_multiplier=D("0.5"),
    )
    yield plan(
        "case_013",
        "two entries already confirmed this session: one slot left",
        [item("A", score="90"), item("B")],
        entries_already_today=2,
    )
    yield plan(
        "case_014",
        "the rung's exposure ceiling: 25% of the sleeve refuses the second 20% position",
        [item("A", score="90", stop="99"), item("B", stop="99")],
        tier=ExposureTier(0, 2, 25.0, new_entries_allowed=True),
    )
    yield plan(
        "case_015",
        "cash runs out: the second line is sized by CASH",
        [item("A", score="90", stop="99"), item("B", stop="99")],
        account=SwingAccount(D("1000000"), D("250000"), frozenset(), D("0")),
        tier=ExposureTier(3, 10, 100.0, new_entries_allowed=True),
    )
    yield plan(
        "case_016",
        "an EP on its gap day lines up like a flag, trigger snapped to the tick",
        [item("EPCO", setup=Setup.EP, trigger="123.37", stop="118.11", score="88")],
    )

    # -- evaluate_trigger: docs/swing/04 §7, the ORH break ---------------------------------------
    day = _dt.date(2026, 9, 2)
    at = _dt.datetime.combine(day, _dt.time(9, 31))
    rng = OpeningRange(D("102.4"), D("99.5"), 5, complete=True, candles=5)

    def trig(name: str, note: str, price: str, **over: Any) -> SwingCase:
        inputs: dict[str, Any] = {
            "last_price": D(price),
            "opening": rng,
            "pivot_high": D("101"),
            "low_of_day": D("99.5"),
            "upper_circuit": None,
            "at": at,
            "config": OpeningRangeConfig(),
        }
        inputs.update(over)
        return SwingCase("baskfy_core.swing.opening_range.evaluate_trigger", name, inputs, note)

    yield trig(
        "case_001",
        "a break of the range high above the pivot triggers, stop at the range low",
        "102.6",
    )
    yield trig("case_002", "a tick over the high is inside the 0.1% buffer: WAITING", "102.45")
    yield trig(
        "case_003",
        "an ORH break below the daily pivot is BELOW_PIVOT",
        "102.6",
        pivot_high=D("105"),
    )
    yield trig("case_004", "an EP has no pivot requirement", "102.6", pivot_high=None)
    yield trig(
        "case_005",
        "at the upper band: LOCKED_UPPER_CIRCUIT, not a fill",
        "105",
        upper_circuit=D("105"),
    )
    yield trig(
        "case_006",
        "an incomplete range never triggers",
        "110",
        opening=OpeningRange(D("102.4"), D("99.5"), 5, complete=False, candles=3),
    )
    yield trig(
        "case_007",
        "after 10:45 the session is over",
        "110",
        at=_dt.datetime.combine(day, _dt.time(10, 46)),
    )
    yield trig(
        "case_008",
        "the stop is the lower of the range low and the day's low",
        "103",
        pivot_high=None,
        low_of_day=D("99.0"),
    )
    yield trig(
        "case_009", "exactly at the buffer (102.4 x 1.001 = 102.5024): 102.50 is WAITING", "102.50"
    )
    yield trig(
        "case_010",
        "at 10:45:00 exactly the monitor is still open",
        "102.6",
        at=_dt.datetime.combine(day, _dt.time(10, 45)),
    )
    yield trig(
        "case_011", "a price exactly on the pivot is not above it", "105", pivot_high=D("105")
    )
    yield trig("case_012", "an upper circuit of zero is no band", "102.6", upper_circuit=D("0"))


def dump_swing() -> list[Path]:
    """The ``swing`` lane: every case, byte-stable, into ``go/testdata/golden/L1/swing/``."""
    written: list[Path] = []
    for case in _swing_cases():
        decode, call = SWING_FUNCTIONS[case.fn]
        # Round-trip the inputs through their JSON form before computing, so the output stored
        # is the output of exactly what the file says the inputs are (and what the drift test
        # will feed back in) — never of an object the encoding could have narrowed.
        encoded_inputs = json.loads(render(_enc(case.inputs)))
        output = call(**decode(encoded_inputs))
        path = swing_case_path(case.fn, case.name)
        written.append(
            dump(
                path.relative_to(GOLDEN_DIR).with_suffix("").as_posix(),
                fn=case.fn,
                inputs=encoded_inputs,
                output=output,
                notes=f"{case.notes}. {SWING_NOTES}",
                stable=True,
            )
        )
    return written


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "swing":
        paths = dump_swing()
        print(f"swing lane: {len(paths)} goldens under {GOLDEN_DIR / SWING_LANE}")
        for p in paths:
            print(f"  {p.relative_to(GOLDEN_DIR)}")
    else:
        print(f"golden dir: {GOLDEN_DIR}")
        print("lanes in this file: swing   (python golden.py swing)")
