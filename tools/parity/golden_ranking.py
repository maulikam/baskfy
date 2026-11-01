"""The ranking lane of the parity goldens: the factor engine, the registry, the screener's query
builder and the ranking engine (Rust rewrite, 29 Oct 2026).

    cd decile-blueprint
    .venv/bin/python ../tools/parity/golden_ranking.py      # or: uv run python ../tools/parity/golden_ranking.py

Dumps the public functions of four ``baskfy_core`` modules into
``go/testdata/golden/L1/ranking/<module>.<function>.case_NNN.json``, written ``stable=True`` by
``tools/parity/golden.py``'s own encoder, so two runs are byte-identical:

* ``factors.compute_factors`` / ``compute_factors_unrounded`` — docs/05 over seeded synthetic bars:
  both benchmarks and neither, adjusted vs raw closes across a split, a short-history listing
  beside a full one, bars *after* ``as_of`` that must not be read (house rule 5), a suspended name
  with no bar on ``as_of``, published circuit bands with locked days, a zero-volatility series,
  every turnover fallback, a one-window config, an empty frame, a missing column, a non-trading
  ``as_of``;
* ``factor_registry`` — the whole table and its constants, ``columns``, ``by_family``,
  ``family_counts``, ``get`` and ``sql_for`` (stored, blend, guarded ratio, computed, unknown);
* ``screener`` — ``validate_definition``, ``resolve_columns``, ``build_screen_query``,
  ``build_survivors_query``, ``build_ranking_frame_query``, ``build_export_query``,
  ``cache_key``, ``canonical_json`` and ``ScreenResult.to_json``. A statement's golden is its
  rendered PostgreSQL text (asyncpg dialect, ``$n`` placeholders), its bound parameters by name,
  and ``param_order`` — the name behind each ``$n`` — so the values stay bound, as in production;
* ``ranking_engine`` — ``family_shares``, ``effective_weights``, ``percentile``,
  ``target_distance``, ``target_score``, ``transform``, ``add_nse_momentum_score``,
  ``rank_frame``, ``rank_history``, ``explain`` and ``explain_instrument``.

Conventions the reimplementation must not misread (restated in every case's ``meta.notes``):

* Every case's inputs are JSON, and every function is called from the stored inputs after a
  round trip through their JSON text — never from an object that was not written down.
* A function that raises stores ``{"raises": {"type", "message"}}`` as its output. A pydantic
  ``ValidationError`` (the ``ScreenDefinition`` model refusing its input) stores
  ``{"type": "ValidationError", "errors": [{"type", "loc", "msg"}]}`` instead of the message,
  which carries a version-specific URL.
* A polars frame is ``{"columns", "dtypes", "rows"}`` (golden.py's encoding); a pandas frame is
  ``{"columns", "dtypes", "index", "rows"}`` with ``pd.NA``/``NaN``/``NaT`` written as ``null``.
* A ``ScreenDefinition`` input is the JSON payload ``ScreenDefinition.model_validate`` takes;
  ``smuggle`` (when present) is applied with ``model_copy(update=...)``, which skips the model's
  validation — the injection tests' way of reaching the query builder's own whitelist.
* ``canonical_json`` payloads tag the two types JSON lacks: ``{"$decimal": "13.00"}`` and
  ``{"$date": "2026-08-18"}``.
* Synthetic bars come from ``numpy.random.default_rng(seed)`` and are rounded to 2 dp before they
  are written, so the stored inputs, not the generator, are the contract.
"""

from __future__ import annotations

import datetime as dt
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


LANE = "L1/ranking"
NOTES = (
    "Inputs are JSON and each case is recomputed from them; a raise is stored as "
    '{"raises": {"type", "message"}} (pydantic: "errors" instead of "message"). Polars frames are '
    "{columns, dtypes, rows}; pandas frames add index and write NA/NaN/NaT as null. Prices: "
    "close is adjusted, close_raw is the exchange print (house rule 6). Regenerate with "
    "`.venv/bin/python ../tools/parity/golden_ranking.py` from decile-blueprint."
)

AS_OF = dt.date(2026, 8, 18)


# ---------------------------------------------------------------------------
# Shared encoding helpers
# ---------------------------------------------------------------------------


def _raised(exc: BaseException) -> dict[str, Any]:
    name = type(exc).__name__
    if name == "ValidationError" and hasattr(exc, "errors"):
        errors = [
            {"type": e["type"], "loc": [str(p) for p in e["loc"]], "msg": e["msg"]}
            for e in exc.errors(include_url=False)
        ]
        return {"raises": {"type": name, "errors": errors}}
    message = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
    return {"raises": {"type": name, "message": str(message)}}


def _capture(call: Callable[[], Any]) -> Any:
    try:
        return call()
    except (ValueError, KeyError, TypeError) as exc:  # ScreenQueryError and pydantic's are ValueErrors
        return _raised(exc)


def _pd_frame(frame: Any) -> dict[str, Any]:
    """A pandas frame in golden.py's shape, with every missing marker written as ``null``."""
    import pandas as pd

    def cell(value: Any) -> Any:
        if value is None or value is pd.NA or value is pd.NaT:
            return None
        if isinstance(value, float) and value != value:  # NaN
            return None
        if isinstance(value, pd.Timestamp):
            return value.isoformat()
        return value

    golden = _golden()
    return {
        "columns": [str(c) for c in frame.columns],
        "dtypes": {str(c): str(t) for c, t in frame.dtypes.items()},
        "index": golden._enc(list(frame.index)),
        "rows": [
            golden._enc([cell(v) for v in row])
            for row in frame.itertuples(index=False, name=None)
        ],
    }


def _pd_series(series: Any) -> dict[str, Any]:
    import pandas as pd

    golden = _golden()
    return {
        "dtype": str(series.dtype),
        "index": golden._enc(list(series.index)),
        "values": golden._enc([None if pd.isna(v) else v for v in series.tolist()]),
    }


def _polars_frame(doc: dict[str, Any]) -> Any:
    return _golden().polars_frame_from(doc)


def _pandas_frame(doc: dict[str, Any]) -> Any:
    """``{"columns", "rows"}`` -> a pandas frame; ``date_columns`` values become ``date``s."""
    import pandas as pd

    dates = set(doc.get("date_columns", []))
    rows = [
        [
            dt.date.fromisoformat(v) if (c in dates and v is not None) else v
            for c, v in zip(doc["columns"], row, strict=True)
        ]
        for row in doc["rows"]
    ]
    return pd.DataFrame(rows, columns=doc["columns"])


def _float_series(values: list[Any]) -> Any:
    import pandas as pd

    return pd.Series([float("nan") if v is None else v for v in values], dtype="float64")


# ---------------------------------------------------------------------------
# factors
# ---------------------------------------------------------------------------

BAR_DTYPES: dict[str, str] = {
    "instrument_id": "Int64",
    "date": "Date",
    "close": "Float64",
    "close_raw": "Float64",
    "high": "Float64",
    "low": "Float64",
    "volume_raw": "Float64",
    "turnover": "Float64",
    "upper_circuit": "Float64",
    "lower_circuit": "Float64",
    "series": "String",
    "marketcap_cr": "Float64",
    "pe": "Float64",
}


def _weekdays(end: dt.date, count: int) -> list[dt.date]:
    days: list[dt.date] = []
    day = end
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= dt.timedelta(days=1)
    return sorted(days)


def _walk(seed: int, n: int, *, start: float = 100.0, drift: float = 0.0008,
          vol: float = 0.018) -> list[float]:  # fmt: skip
    import numpy as np

    rng = np.random.default_rng(seed)
    price = start
    out: list[float] = []
    for step in rng.normal(drift, vol, size=n):
        price = max(round(price * (1.0 + float(step)), 2), 0.05)
        out.append(price)
    return out


def _volumes(seed: int, n: int) -> list[float]:
    import numpy as np

    rng = np.random.default_rng(seed + 10_000)
    return [float(v) for v in rng.integers(20_000, 400_000, size=n)]


def _instrument_rows(  # noqa: PLR0913 - one keyword per way a fixture varies
    instrument_id: int,
    days: list[dt.date],
    closes: list[float],
    *,
    seed: int,
    columns: list[str],
    raw_factor: Callable[[int], float] = lambda _i: 1.0,
    turnover: Callable[[int, float, float], float | None] | None = None,
    band: float | None = None,
    series: str | None = "EQ",
    marketcap_cr: float | None = None,
    pe: float | None = None,
) -> list[list[Any]]:
    """Rows in ``columns`` order. ``raw_factor(i)`` scales the raw print over the adjusted close
    (2.0 before a 2:1 split, 1.0 after) — house rule 6's two series."""
    volumes = _volumes(seed, len(days))
    rows: list[list[Any]] = []
    for i, (day, close) in enumerate(zip(days, closes, strict=True)):
        raw = round(close * raw_factor(i), 2)
        prev_raw = round(closes[i - 1] * raw_factor(i - 1), 2) if i else raw
        values: dict[str, Any] = {
            "instrument_id": instrument_id,
            "date": day.isoformat(),
            "close": close,
            "close_raw": raw,
            "high": round(close * 1.012, 2),
            "low": round(close * 0.988, 2),
            "volume_raw": volumes[i],
            "turnover": None if turnover is None else turnover(i, raw, volumes[i]),
            "upper_circuit": None if band is None else round(prev_raw * (1 + band), 2),
            "lower_circuit": None if band is None else round(prev_raw * (1 - band), 2),
            "series": series,
            "marketcap_cr": marketcap_cr,
            "pe": pe,
        }
        rows.append([values[c] for c in columns])
    return rows


def _bars_doc(columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {"columns": columns, "dtypes": {c: BAR_DTYPES[c] for c in columns}, "rows": rows}


def _benchmark_doc(seed: int, days: list[dt.date], column: str = "close") -> dict[str, Any]:
    levels = _walk(seed, len(days), start=24000.0, drift=0.0004, vol=0.009)
    return {
        "columns": ["date", column],
        "dtypes": {"date": "Date", column: "Float64"},
        "rows": [[d.isoformat(), v] for d, v in zip(days, levels, strict=True)],
    }


def _factor_config_doc(**overrides: Any) -> dict[str, Any]:
    from baskfy_core.factors import FactorConfig

    config = FactorConfig(**overrides)
    golden = _golden()
    return json.loads(golden.render(golden._enc(config)))


def _factor_config(doc: dict[str, Any]) -> Any:
    from baskfy_core.circuits import CircuitConfig
    from baskfy_core.factors import FactorConfig
    from baskfy_core.instrument_regime import RegimeConfig
    from baskfy_core.momentum import SkipMonthConfig, SkipMonthDefinition

    circuits = dict(doc["circuits"])
    circuits["bands"] = tuple(circuits["bands"])
    skip = dict(doc["skip_month"])
    skip["definition"] = SkipMonthDefinition(skip["definition"])
    return FactorConfig(
        circuits=CircuitConfig(**circuits),
        skip_month=SkipMonthConfig(**skip),
        regime=RegimeConfig(**doc["regime"]),
        window_months=tuple(doc["window_months"]),
        min_beta_observations=int(doc["min_beta_observations"]),
    )


def _factor_result(result: Any) -> dict[str, Any]:
    windows = {
        str(months): {
            "months": w.months,
            "as_of": w.as_of,
            "calendar_start": w.calendar_start,
            "start": w.start,
            "end": w.trading_days[-1] if w.trading_days else None,
            "length": len(w.trading_days),
            "calendar_first": w.calendar_first,
            "spans_full_window": w.spans_full_window,
        }
        for months, w in sorted(result.windows.items())
    }
    return {"frame": result.frame, "windows": windows}


def _call_factors(inputs: dict[str, Any], *, rounded: bool) -> Any:
    from baskfy_core.factors import compute_factors, compute_factors_unrounded

    fn = compute_factors if rounded else compute_factors_unrounded

    def run() -> Any:
        bench = inputs["benchmark"]
        market = inputs["market_benchmark"]
        return _factor_result(
            fn(
                _polars_frame(inputs["bars"]),
                dt.date.fromisoformat(inputs["as_of"]),
                [dt.date.fromisoformat(d) for d in inputs["trading_days"]],
                None if bench is None else _polars_frame(bench),
                _factor_config(inputs["config"]),
                market_benchmark=None if market is None else _polars_frame(market),
            )
        )

    return _capture(run)


def call_compute_factors(inputs: dict[str, Any]) -> Any:
    return _call_factors(inputs, rounded=True)


def call_compute_factors_unrounded(inputs: dict[str, Any]) -> Any:
    return _call_factors(inputs, rounded=False)


FULL_COLUMNS = list(BAR_DTYPES)
REQUIRED_ONLY = ["instrument_id", "date", "close", "close_raw", "high", "low", "volume_raw"]


def _factor_cases() -> list[tuple[str, dict[str, Any]]]:  # noqa: PLR0915 - one block per case
    """``(description, inputs)`` for compute_factors, in a fixed order."""
    n = 280
    days = _weekdays(AS_OF, n)
    day_strs = [d.isoformat() for d in days]
    full = _factor_config_doc()
    bench = _benchmark_doc(901, days)
    market = _benchmark_doc(902, days)
    exch = lambda _i, raw, vol: round(raw * vol * 1.003, 2)  # noqa: E731

    def three() -> list[list[Any]]:
        return [
            *_instrument_rows(101, days, _walk(1, n), seed=1, columns=FULL_COLUMNS, turnover=exch,
                              marketcap_cr=42000.0, pe=31.5),
            *_instrument_rows(102, days, _walk(2, n, drift=-0.0005), seed=2, columns=FULL_COLUMNS,
                              marketcap_cr=1800.0, pe=None),
            *_instrument_rows(103, days, _walk(3, n, vol=0.032), seed=3, columns=FULL_COLUMNS,
                              series="BE", marketcap_cr=None, pe=-4.2),
        ]  # fmt: skip

    cases: list[tuple[str, dict[str, Any]]] = []

    def add(why: str, bars: dict[str, Any], **kw: Any) -> None:
        inputs = {
            "bars": bars,
            "as_of": kw.get("as_of", AS_OF.isoformat()),
            "trading_days": kw.get("trading_days", day_strs),
            "benchmark": kw.get("benchmark", bench),
            "market_benchmark": kw.get("market_benchmark", market),
            "config": kw.get("config", full),
        }
        cases.append((why, inputs))

    add("three names, 280 sessions, both benchmarks; exchange turnover on 101, close x volume on "
        "102/103; 103 is BE with negative P/E and no marketcap", _bars_doc(FULL_COLUMNS, three()))  # fmt: skip
    add("the same bars with neither benchmark: beta, resid and excess returns are NULL",
        _bars_doc(FULL_COLUMNS, three()), benchmark=None, market_benchmark=None)

    # Adjusted vs raw: a 2:1 split at bar 150. close is smooth; close_raw halves on the ex-date.
    split_at = 150
    closes = _walk(4, n)
    add("adjusted vs raw: 2:1 split at bar 150 (close smooth, close_raw halves); published 20% "
        "bands on 201, none on 202 so the raw halving meets the band heuristic",
        _bars_doc(FULL_COLUMNS, [
            *_instrument_rows(201, days, closes, seed=4, columns=FULL_COLUMNS, band=0.20,
                              raw_factor=lambda i: 2.0 if i < split_at else 1.0),
            *_instrument_rows(202, days, closes, seed=5, columns=FULL_COLUMNS,
                              raw_factor=lambda i: 2.0 if i < split_at else 1.0),
            *_instrument_rows(203, days, closes, seed=6, columns=FULL_COLUMNS),
        ]))  # fmt: skip

    add("a 40-session listing beside a full history: every window longer than its bars is NULL",
        _bars_doc(FULL_COLUMNS, [
            *_instrument_rows(301, days, _walk(7, n), seed=7, columns=FULL_COLUMNS),
            *_instrument_rows(302, days[-40:], _walk(8, 40, start=250.0), seed=8,
                              columns=FULL_COLUMNS),
        ]))  # fmt: skip

    # House rule 5: ten sessions after as_of, with a tenfold spike, are in the frame and the
    # calendar; none may move an as_of value. 402 is suspended five sessions before as_of.
    future = [AS_OF + dt.timedelta(days=k) for k in range(1, 20) if (AS_OF + dt.timedelta(days=k)).weekday() < 5][:10]  # noqa: E501
    all_days = days + future
    spike = _walk(9, n) + [9999.0] * len(future)
    add("no look-ahead: ten sessions after as_of (a tenfold spike) are in the bars and the "
        "calendar and must not reach any as_of value; 402's last bar is five sessions before "
        "as_of so it has no output row",
        _bars_doc(FULL_COLUMNS, [
            *_instrument_rows(401, all_days, spike, seed=9, columns=FULL_COLUMNS),
            *_instrument_rows(402, days[:-5], _walk(10, n - 5), seed=10, columns=FULL_COLUMNS),
        ]), trading_days=[d.isoformat() for d in all_days],
        benchmark=_benchmark_doc(901, all_days), market_benchmark=_benchmark_doc(902, all_days))  # fmt: skip

    # Published circuit bands with three locked sessions on 501 (high == low == close_raw == band).
    locked_closes = _walk(11, n)
    for pos in (120, 200, 270):
        locked_closes[pos] = round(locked_closes[pos - 1] * 1.05, 2)
    rows501 = _instrument_rows(501, days, locked_closes, seed=11, columns=FULL_COLUMNS, band=0.05)
    idx = {c: FULL_COLUMNS.index(c) for c in FULL_COLUMNS}
    for pos in (120, 200, 270):
        row = rows501[pos]
        row[idx["high"]] = row[idx["low"]] = row[idx["close_raw"]]
        row[idx["upper_circuit"]] = row[idx["close_raw"]]
    add("published 5% bands with three locked-limit sessions (inside 12m, 3m and 1m)",
        _bars_doc(FULL_COLUMNS, [
            *rows501,
            *_instrument_rows(502, days, _walk(12, n), seed=12, columns=FULL_COLUMNS, band=0.05),
        ]))  # fmt: skip

    add("a constant price: zero volatility, so every sharpe is NULL, beside a moving name",
        _bars_doc(FULL_COLUMNS, [
            *_instrument_rows(601, days, [250.0] * n, seed=13, columns=FULL_COLUMNS),
            *_instrument_rows(602, days, _walk(14, n), seed=14, columns=FULL_COLUMNS),
        ]))  # fmt: skip

    def odd_turnover(i: int, raw: float, vol: float) -> float | None:
        if i == n - 1:
            return 0.0  # a session that printed nothing is unavailable: fall back
        if i == n - 2:
            return 0.4  # sub-rupee but positive: used as printed
        if i % 7 == 0:
            return None
        return round(raw * vol, 2)

    add("turnover fallbacks: zero on as_of, sub-rupee the day before, NULL every seventh bar; "
        "and the optional columns (bands, series, marketcap, pe) absent from the frame",
        _bars_doc([*REQUIRED_ONLY, "turnover"], _instrument_rows(
            701, days, _walk(15, n), seed=15, columns=[*REQUIRED_ONLY, "turnover"],
            turnover=odd_turnover)))  # fmt: skip

    short_days = _weekdays(AS_OF, 45)
    add("a one-window config (window_months=(1,), min_beta_observations=20) over 45 sessions; "
        "the NIFTY 50 benchmark spelled 'level'",
        _bars_doc(REQUIRED_ONLY, [
            *_instrument_rows(801, short_days, _walk(16, 45), seed=16, columns=REQUIRED_ONLY),
            *_instrument_rows(802, short_days, _walk(17, 45), seed=17, columns=REQUIRED_ONLY),
        ]), trading_days=[d.isoformat() for d in short_days],
        benchmark=_benchmark_doc(903, short_days, "level"), market_benchmark=None,
        config=_factor_config_doc(window_months=(1,), min_beta_observations=20))  # fmt: skip

    add("an empty frame with every required column", _bars_doc(REQUIRED_ONLY, []))
    add("close_raw missing: refused before anything is computed",
        _bars_doc([c for c in REQUIRED_ONLY if c != "close_raw"], []))
    add("as_of on a Saturday, absent from the calendar: refused",
        _bars_doc(REQUIRED_ONLY, _instrument_rows(901, days, _walk(18, n), seed=18,
                                                  columns=REQUIRED_ONLY)),
        as_of="2026-08-15")  # fmt: skip
    return cases


# ---------------------------------------------------------------------------
# factor_registry
# ---------------------------------------------------------------------------


def call_registry_table(_inputs: dict[str, Any]) -> Any:
    from baskfy_core import factor_registry as fr

    return {
        "factors": [fr.FACTORS[k] for k in fr.FACTORS],
        "DOCUMENTED_FACTOR_COUNT": fr.DOCUMENTED_FACTOR_COUNT,
        "NAMED_FACTOR_COUNT": fr.NAMED_FACTOR_COUNT,
        "DOCUMENTED_COLUMN_COUNT": fr.DOCUMENTED_COLUMN_COUNT,
        "SORT_FACTOR_KEYS": fr.SORT_FACTOR_KEYS,
        "CUSTOM_FILTER_OPERANDS": fr.CUSTOM_FILTER_OPERANDS,
        "COLUMN_PICKER_KEYS": fr.COLUMN_PICKER_KEYS,
        "BACKTEST_SIGNAL_KEYS": fr.BACKTEST_SIGNAL_KEYS,
        "REGIME_PRIORITY_SQL": fr.REGIME_PRIORITY_SQL,
    }


def call_columns(_inputs: dict[str, Any]) -> Any:
    from baskfy_core.factor_registry import columns

    return list(columns())


def call_by_family(_inputs: dict[str, Any]) -> Any:
    from baskfy_core.factor_registry import by_family

    return {family.value: [f.key for f in items] for family, items in by_family().items()}


def call_family_counts(_inputs: dict[str, Any]) -> Any:
    from baskfy_core.factor_registry import family_counts

    return family_counts()


def call_get(inputs: dict[str, Any]) -> Any:
    from baskfy_core.factor_registry import get

    return _capture(lambda: get(inputs["key"]))


def call_sql_for(inputs: dict[str, Any]) -> Any:
    from baskfy_core.factor_registry import sql_for

    return _capture(lambda: sql_for(inputs["key"]))


REGISTRY_KEYS = (
    "ret_12m",  # stored column
    "avg_sharpe_12_6_3",  # blend
    "sharpe_div_beta_12m",  # beta-guarded ratio
    "ma_dist_50",  # target-range expression
    "regime_priority",  # CASE expression
    "atr_14",  # stored, not rankable
    "desk_score",  # computed
    "nse_momentum_score",  # computed
    "ret_12m; DROP TABLE factor_daily",  # injection
    "",  # empty
)


# ---------------------------------------------------------------------------
# screener
# ---------------------------------------------------------------------------


def _query_doc(query: Any) -> dict[str, Any]:
    compiled = query.statement.compile(dialect=_postgres())
    doc: dict[str, Any] = {
        "sql": str(compiled),
        "params": dict(compiled.params),
        "param_order": _param_order(compiled),
        "as_of": query.as_of,
        "index_id": query.index_id,
        "universe_slug": query.universe_slug,
        "definition_hash": query.definition_hash,
    }
    for name in ("columns", "row_columns", "sorting_factor", "ranking_factors", "term_columns"):
        if hasattr(query, name):
            doc[name] = getattr(query, name)
    if hasattr(query, "clause_details"):
        doc["clause_details"] = [[k, v] for k, v in query.clause_details.items()]
        doc["fail_columns"] = query.fail_columns
    return doc


def _param_order(compiled: Any) -> list[str]:
    """The asyncpg dialect renders ``$1, $2, ...``: the bind name behind each position, in order."""
    return list(compiled.positiontup or [])


def _postgres() -> Any:
    from baskfy_core import screener

    return screener._POSTGRES


def call_validate_definition(inputs: dict[str, Any]) -> Any:
    from baskfy_core.screener import validate_definition

    return _capture(lambda: {"valid": validate_definition(_definition(inputs)) is None})


def call_resolve_columns(inputs: dict[str, Any]) -> Any:
    from baskfy_core.screener import resolve_columns

    return _capture(lambda: resolve_columns(inputs["saved"]))


def call_build_screen_query(inputs: dict[str, Any]) -> Any:
    from baskfy_core.screener import build_screen_query

    def run() -> Any:
        kw: dict[str, Any] = {"columns": inputs.get("columns", [])}
        if inputs.get("limit") is not None:
            kw["limit"] = inputs["limit"]
        return _query_doc(
            build_screen_query(_definition(inputs), dt.date.fromisoformat(inputs["as_of"]), **kw)
        )

    return _capture(run)


def call_build_survivors_query(inputs: dict[str, Any]) -> Any:
    from baskfy_core.screener import build_survivors_query

    def run() -> Any:
        kw: dict[str, Any] = {"columns": inputs.get("columns", [])}
        if inputs.get("limit") is not None:
            kw["limit"] = inputs["limit"]
        return _query_doc(
            build_survivors_query(_definition(inputs), dt.date.fromisoformat(inputs["as_of"]), **kw)
        )

    return _capture(run)


def call_build_ranking_frame_query(inputs: dict[str, Any]) -> Any:
    from baskfy_core.screener import build_ranking_frame_query

    return _capture(
        lambda: _query_doc(
            build_ranking_frame_query(_definition(inputs), dt.date.fromisoformat(inputs["as_of"]))
        )
    )


def call_build_export_query(inputs: dict[str, Any]) -> Any:
    from baskfy_core.screener import build_export_query

    def run() -> Any:
        kw: dict[str, Any] = {}
        if inputs.get("limit") is not None:
            kw["limit"] = inputs["limit"]
        statement = build_export_query(
            _definition(inputs), dt.date.fromisoformat(inputs["as_of"]), **kw
        )
        compiled = statement.compile(dialect=_postgres())
        return {"sql": str(compiled), "params": dict(compiled.params),
                "param_order": _param_order(compiled)}  # fmt: skip

    return _capture(run)


def call_cache_key(inputs: dict[str, Any]) -> Any:
    from baskfy_core.screener import cache_key

    return _capture(
        lambda: cache_key(
            _definition(inputs),
            dt.date.fromisoformat(inputs["as_of"]),
            int(inputs["data_version"]),
            columns=inputs.get("columns", []),
        )
    )


def _untag(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$decimal"}:
            return Decimal(value["$decimal"])
        if set(value) == {"$date"}:
            return dt.date.fromisoformat(value["$date"])
        if set(value) == {"$object"}:
            return object()
        return {k: _untag(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_untag(v) for v in value]
    return value


def call_canonical_json(inputs: dict[str, Any]) -> Any:
    from baskfy_core.screener import canonical_json

    return _capture(lambda: canonical_json(_untag(inputs["payload"])))


def call_screen_result_to_json(inputs: dict[str, Any]) -> Any:
    from baskfy_core.screener import RankingFactor, ScreenResult, ScreenResultRow

    def run() -> Any:
        sf = inputs["sorting_factor"]
        result = ScreenResult(
            as_of=dt.date.fromisoformat(inputs["as_of"]),
            data_version=int(inputs["data_version"]),
            sorting_factor=RankingFactor(sf["position"], sf["key"], sf["label"], sf["direction"]),
            columns=tuple(inputs["columns"]),
            rows=tuple(
                ScreenResultRow(
                    rank=r["rank"],
                    instrument_id=r["instrument_id"],
                    combined_rank=r["combined_rank"],
                    ranks=tuple(r["ranks"]),
                    values=_untag(r["values"]),
                )
                for r in inputs["rows"]
            ),
        )
        return {"to_json": result.to_json(), "result_count": result.result_count,
                "symbols": result.symbols}  # fmt: skip

    return _capture(run)


BARE = {"index": "nifty-500", "sort_by": "ret_12m"}
EVERY_FILTER = {
    "index": "nifty-500",
    "sort_by": "avg_sharpe_12_6_3_1",
    "apply_filters_on": "decile_1",
    "min_return_1y": "10.5",
    "median_volume_1y": 10000000,
    "moving_average": {"enabled": True, "above_200": True, "above_50": True, "below_20": True},
    "away_from_high": {"ath": 25, "one_year": 30},
    "positive_days": {"m12": 50, "m1": 40},
    "circuits": {"m12": 5, "m1": 250},
    "marketcap": {"from": "500", "to": "50000.00"},
    "pe": {"enabled": True, "from": "0", "to": "40"},
    "series": ["EQ", "BE"],
    "ignore_top_beta": {"enabled": True, "count": 0},
    "ignore_top_volatility": {"enabled": True, "count": 10},
    "ignore_above_beta": 2,
    "price": {"from": "50"},
    "historical_date": "2026-08-14",
}
THREE_FACTORS = {
    "index": "nifty-midcap-150",
    "sort_by": "ret_6m",
    "sort_direction": "asc",
    "apply_filters_on": "top_50",
    "ranking_mode": "sequential",
    "factor_two": {"enabled": True, "sort_by": "vol_12m", "sort_direction": "asc"},
    "factor_three": {"enabled": True, "sort_by": "rsi_3m", "sort_direction": "desc"},
}
COMBINED_TWO = {
    "index": "nifty-200",
    "sort_by": "sharpe_12m",
    "factor_two": {"enabled": True, "sort_by": "pos_days_12m"},
    "factor_three": {"enabled": False, "sort_by": "rsi_1m"},
}
DESK = {"index": "nifty-500", "sort_by": "desk_score"}
CUSTOM = {
    "index": "nifty-total-market",
    "sort_by": "ret_3m",
    "custom_filters": [
        {"enabled": True, "left": "ma_50", "op": ">=", "right": "ma_200"},
        {"enabled": False, "left": "close", "op": "<=", "right": "ma_20"},
        {"enabled": True, "left": "vol_avg_1w", "op": "=", "right": "vol_avg_12m"},
    ],
}
RANGES = {
    "index": "nifty-smallcap-250",
    "sort_by": "eff_ratio_63",
    "factor_ranges": [
        {"factor": "excess_ret_12m", "min": 0},
        {"factor": "atr_14", "max": 12.5},
        {"enabled": False, "factor": "beta_12m", "min": 0.5, "max": 1.5},
        {"factor": "ma_dist_50", "min": -5, "max": 15},
    ],
    "regime_in": ["BULL", "NEUTRAL"],
}
TERMS = {
    "index": "nifty-500",
    "sort_by": "ret_12m",
    "ranking_mode": "composite",
    "ranking_scope": "filtered_results",
    "missing_data": "neutral",
    "ranking_terms": [
        {"factor": "ret_12m", "preference": "higher", "weight": 2},
        {"factor": "nse_momentum_score", "preference": "higher"},
        {"factor": "desk_score", "preference": "higher"},
        {"factor": "ma_dist_50", "preference": "target_range", "target_min": 0, "target_max": 15},
        {"factor": "eff_ratio_63", "preference": "higher"},
        {"factor": "vol_12m", "preference": "lower", "weight": 0.5},
    ],
    "family_weights": {"momentum": 60, "path_quality": 20},
    "min_return_1y": "0",
}
SECTOR_TERMS = {
    "index": "nifty-mid-small-400",
    "sort_by": "sharpe_12m",
    "apply_filters_on": "decile_3",
    "ranking_mode": "composite",
    "ranking_scope": "within_sector",
    "ranking_terms": [
        {"factor": "sharpe_12m", "preference": "higher"},
        {"factor": "avg_rsi_12_6_3", "preference": "higher"},
    ],
    "moving_average": {"enabled": True, "above_200": True},
}
NSE_SORT = {"index": "nifty-200", "sort_by": "nse_momentum_score"}
SME = {"index": "nse-sme-emerge", "sort_by": "ret_1m", "series": ["SM", "ST"]}
ETF = {"index": "etf", "sort_by": "avg_return_12_9_6", "sort_direction": "asc", "series": [],
       "apply_filters_on": "decile_5"}  # fmt: skip


def _screener_cases() -> list[tuple[str, str, dict[str, Any]]]:  # noqa: PLR0915 - a flat table
    s = "baskfy_core.screener."
    asof = AS_OF.isoformat()
    out: list[tuple[str, str, dict[str, Any]]] = []

    def add(fn: str, why: str, inputs: dict[str, Any]) -> None:
        out.append((s + fn, why, inputs))

    for why, inputs in (
        ("a bare definition", {"definition": BARE}),
        ("every docs/06 filter active", {"definition": EVERY_FILTER}),
        ("custom filters, one slot disabled", {"definition": CUSTOM}),
        ("explicit ranking terms and factor ranges", {"definition": TERMS}),
        ("smuggled sort_by past the model", {"definition": BARE,
                                             "smuggle": {"sort_by": "ret_12m) OR 1=1 --"}}),
        ("smuggled custom-filter operand (a blend)", {"definition": BARE, "smuggle": {
            "custom_filters": [{"enabled": True, "left": "avg_sharpe_12_6_3", "op": ">=",
                                "right": "ma_200"}]}}),
        ("smuggled operand on a disabled slot is never looked at", {"definition": BARE,
            "smuggle": {"custom_filters": [{"enabled": False, "left": "pg_sleep(10)", "op": ">=",
                                            "right": "ma_200"}]}}),
        ("smuggled unknown bucket", {"definition": BARE,
                                     "smuggle": {"apply_filters_on": "decile_9"}}),
        ("smuggled factor_two key while enabled", {"definition": COMBINED_TWO, "smuggle": {
            "factor_two": {"enabled": True, "sort_by": "nope", "sort_direction": "desc"}}}),
        ("the model refuses factor_three without factor_two", {"definition": {
            **BARE, "factor_three": {"enabled": True, "sort_by": "rsi_1m"}}}),
        ("the model refuses an unknown key (extra=forbid)", {"definition": {**BARE, "colour": 1}}),
    ):  # fmt: skip
        add("validate_definition", why, inputs)

    for why, saved in (
        ("no saved columns: identity then defaults", []),
        ("saved extras keep the user's order", ["ma_200", "series", "rsi_3m"]),
        ("defaults and repeats collapse", ["ret_12m", "close_raw", "beta_12m", "beta_12m"]),
        ("every column-picker key", None),
        ("an unknown column is refused, not dropped", ["ma_200", "colour"]),
        ("a factor that is not a picker column is refused", ["avg_sharpe_12_6_3"]),
    ):
        if saved is None:
            from baskfy_core.factor_registry import COLUMN_PICKER_KEYS

            saved = list(COLUMN_PICKER_KEYS)
        add("resolve_columns", why, {"saved": saved})

    for why, inputs in (
        ("a bare definition", {"definition": BARE}),
        ("saved columns and a LIMIT of 25", {"definition": BARE,
                                             "columns": ["ma_200", "series", "circuits_1m"],
                                             "limit": 25}),
        ("every docs/06 filter, top decile by marketcap", {"definition": EVERY_FILTER}),
        ("three factors, sequential, top_50 rows, mixed directions", {"definition": THREE_FACTORS}),
        ("factor two enabled, factor three disabled (sum of two ranks)",
         {"definition": COMBINED_TWO}),
        ("desk_score read from desk_score_daily", {"definition": DESK}),
        ("custom filters, one disabled", {"definition": CUSTOM}),
        ("factor ranges and regime_in on the legacy path", {"definition": RANGES}),
        ("SME universe derived by series", {"definition": SME}),
        ("ETF universe, ascending blend, no series filter, decile_5", {"definition": ETF}),
        ("ranking_terms refuse the legacy SQL", {"definition": TERMS}),
        ("a computed factor (nse_momentum_score) cannot rank in SQL", {"definition": NSE_SORT}),
        ("a smuggled sort key never reaches the SQL", {"definition": BARE,
                                                       "smuggle": {"sort_by": "pg_sleep(10)"}}),
        ("an unknown result column is refused", {"definition": BARE, "columns": ["colour"]}),
    ):  # fmt: skip
        add("build_screen_query", why, {"as_of": asof, **inputs})

    for why, inputs in (
        ("desk_score survivors", {"definition": DESK}),
        ("desk_score survivors after every filter, decile_2", {"definition": {
            **EVERY_FILTER, "sort_by": "desk_score", "apply_filters_on": "decile_2"}}),
        ("desk_score with saved columns and a limit", {"definition": DESK,
                                                       "columns": ["ma_200", "rsi_1m"], "limit": 50}),
        ("desk_score over top_100 with custom filters", {"definition": {
            **CUSTOM, "sort_by": "desk_score", "apply_filters_on": "top_100"}}),
        ("a non-desk sort is refused", {"definition": BARE}),
    ):  # fmt: skip
        add("build_survivors_query", why, {"as_of": asof, **inputs})

    for why, inputs in (
        ("the legacy definition's frame", {"definition": BARE}),
        ("composite terms: nse_momentum_score and desk_score are computed, ma_dist_50 is an "
         "expression, one fail__ flag per filter", {"definition": TERMS}),
        ("within_sector over decile_3 with an MA filter", {"definition": SECTOR_TERMS}),
        ("every docs/06 filter and risk flag as fail__ columns", {"definition": EVERY_FILTER}),
        ("factor ranges and regime_in as fail__ columns", {"definition": RANGES}),
        ("three legacy factors: the expression terms are projected", {"definition": THREE_FACTORS}),
    ):  # fmt: skip
        add("build_ranking_frame_query", why, {"as_of": asof, **inputs})

    for why, inputs in (
        ("a bare definition", {"definition": BARE}),
        ("every docs/06 filter", {"definition": EVERY_FILTER, "limit": 100}),
        ("three factors, sequential", {"definition": THREE_FACTORS}),
        ("ETF universe, ascending blend", {"definition": ETF}),
        ("ranking_terms are refused", {"definition": TERMS}),
    ):
        add("build_export_query", why, {"as_of": asof, **inputs})

    for why, inputs in (
        ("bare definition, version 1", {"definition": BARE, "data_version": 1}),
        ("same definition, a later data_version", {"definition": BARE, "data_version": 7}),
        ("an explicit default spells the same key", {"definition": {
            **BARE, "sort_direction": "desc", "series": ["EQ"]}, "data_version": 1}),
        ("columns change the digest", {"definition": BARE, "data_version": 1,
                                       "columns": ["ma_200"]}),
        ("a default column named explicitly does not", {"definition": BARE, "data_version": 1,
                                                        "columns": ["ret_12m"]}),
        ("another as_of", {"definition": BARE, "data_version": 1, "as_of": "2026-08-17"}),
        ("every filter", {"definition": EVERY_FILTER, "data_version": 3}),
        ("an unknown column is refused", {"definition": BARE, "data_version": 1,
                                          "columns": ["colour"]}),
    ):  # fmt: skip
        add("cache_key", why, {"as_of": asof, **inputs})

    for why, payload in (
        ("decimals keep their stored precision", {"sharpe_12m": {"$decimal": "13.00"},
                                                  "ret": {"$decimal": "-0.50"}}),
        ("exponent decimals are expanded", [{"$decimal": "1E+3"}, {"$decimal": "1.5E-7"},
                                            {"$decimal": "0E-2"}]),
        ("negative zero", [{"$decimal": "-0"}, {"$decimal": "-0.00"}]),
        ("dates, bools, null, ints, floats, unicode", {"d": {"$date": "2026-08-18"}, "t": True,
                                                       "f": False, "n": None, "i": 42, "x": 0.1,
                                                       "s": "Bajaj Auto — ₹"}),
        ("insertion order is kept, not sorted", {"z": 1, "a": 2, "m": [3, 2, 1]}),
        ("nested", {"rows": [{"rank": 1, "v": {"$decimal": "2.10"}}], "empty": [], "obj": {}}),
        ("a NaN decimal is refused", [{"$decimal": "NaN"}]),
        ("an infinite decimal is refused", [{"$decimal": "Infinity"}]),
        ("an unencodable object is refused", {"x": {"$object": True}}),
    ):  # fmt: skip
        add("canonical_json", why, {"payload": payload})

    result_rows = [
        {"rank": 1, "instrument_id": 11, "combined_rank": 1, "ranks": [1, 0, 0],
         "values": {"symbol": "CUPID", "name": "Cupid Ltd", "sorting_factor": {"$decimal": "726.63"},
                    "ret_12m": {"$decimal": "726.63"}, "vol_12m": {"$decimal": "0.5793000000"},
                    "close_raw": {"$decimal": "112.40"}}},
        {"rank": 2, "instrument_id": 7, "combined_rank": 2, "ranks": [2, 0, 0],
         "values": {"symbol": "ABC", "name": "A B C", "sorting_factor": None, "ret_12m": None,
                    "vol_12m": {"$decimal": "1E-10"}, "close_raw": {"$decimal": "5"}}},
    ]  # fmt: skip
    base_result = {"as_of": asof, "data_version": 4,
                   "sorting_factor": {"position": 1, "key": "ret_12m",
                                      "label": "ABSOLUTE RETURN 1 YEAR", "direction": "desc"},
                   "columns": ["symbol", "name", "sorting_factor", "ret_12m", "vol_12m",
                               "close_raw"]}  # fmt: skip
    add("ScreenResult.to_json", "two rows, decimals at storage precision, a NULL factor",
        {**base_result, "rows": result_rows})
    add("ScreenResult.to_json", "no rows", {**base_result, "rows": []})
    return out


# model_copy does not validate, so nested dicts would stay dicts; rebuild the nested models here.
def _smuggle_value(key: str, value: Any) -> Any:
    from baskfy_core.screen_definition import CustomFilter, ExtraFactor

    if key == "custom_filters":
        return [CustomFilter.model_construct(**v) for v in value]
    if key in ("factor_two", "factor_three"):
        return ExtraFactor.model_construct(**value)
    return value


def _definition(inputs: dict[str, Any]) -> Any:
    from baskfy_core.screen_definition import ScreenDefinition

    definition = ScreenDefinition.model_validate(inputs["definition"])
    smuggle = inputs.get("smuggle")
    if smuggle:
        definition = definition.model_copy(
            update={k: _smuggle_value(k, v) for k, v in smuggle.items()}
        )
    return definition


# ---------------------------------------------------------------------------
# ranking_engine
# ---------------------------------------------------------------------------


def _spec(doc: dict[str, Any]) -> Any:
    from baskfy_core.ranking_engine import RankingSpec, TermSpec

    terms = tuple(TermSpec(**t) for t in doc["terms"])
    return RankingSpec(
        terms=terms,
        mode=doc["mode"],
        scope=doc["scope"],
        missing_data=doc.get("missing_data", "penalize"),
        family_weights=doc.get("family_weights"),
    )


def call_family_shares(inputs: dict[str, Any]) -> Any:
    from baskfy_core.ranking_engine import family_shares

    return _capture(lambda: family_shares(_spec(inputs["spec"])))


def call_effective_weights(inputs: dict[str, Any]) -> Any:
    from baskfy_core.ranking_engine import effective_weights

    return _capture(lambda: effective_weights(_spec(inputs["spec"])))


def call_percentile(inputs: dict[str, Any]) -> Any:
    from baskfy_core.ranking_engine import percentile

    return _pd_series(percentile(_float_series(inputs["values"])))


def call_target_distance(inputs: dict[str, Any]) -> Any:
    from baskfy_core.ranking_engine import target_distance

    return _pd_series(
        target_distance(_float_series(inputs["values"]), inputs["lower"], inputs["upper"])
    )


def call_target_score(inputs: dict[str, Any]) -> Any:
    from baskfy_core.ranking_engine import target_score

    return _pd_series(target_score(_float_series(inputs["distances"])))


def call_transform(inputs: dict[str, Any]) -> Any:
    from baskfy_core.ranking_engine import TermSpec, transform

    return _capture(
        lambda: _pd_series(transform(_float_series(inputs["values"]), TermSpec(**inputs["term"])))
    )


def call_add_nse_momentum_score(inputs: dict[str, Any]) -> Any:
    from baskfy_core.ranking_engine import add_nse_momentum_score

    return _capture(lambda: _pd_frame(add_nse_momentum_score(_pandas_frame(inputs["frame"]))))


def call_rank_frame(inputs: dict[str, Any]) -> Any:
    from baskfy_core.ranking_engine import rank_frame

    def run() -> Any:
        result = rank_frame(_pandas_frame(inputs["frame"]), _spec(inputs["spec"]))
        return {"ranked": _pd_frame(result.ranked), "scored": _pd_frame(result.scored),
                "weights": dict(result.weights)}  # fmt: skip

    return _capture(run)


def call_rank_history(inputs: dict[str, Any]) -> Any:
    from baskfy_core.ranking_engine import rank_history

    prev = inputs["previous_as_of"]
    return rank_history(
        inputs["today"], inputs["previous"], None if prev is None else dt.date.fromisoformat(prev)
    )


def _context(inputs: dict[str, Any]) -> Any:
    from baskfy_core.ranking_engine import ExplainContext, rank_frame

    c = inputs["context"]
    prev = c.get("previous_as_of")
    return ExplainContext(
        result=rank_frame(_pandas_frame(inputs["frame"]), _spec(inputs["spec"])),
        universe=c["universe"],
        as_of=dt.date.fromisoformat(c["as_of"]),
        data_version=c.get("data_version"),
        desk_score_version=c.get("desk_score_version"),
        clause_details=dict(c.get("clause_details", {})),
        previous_as_of=None if prev is None else dt.date.fromisoformat(prev),
        previous_rank=c.get("previous_rank"),
    )


def call_explain(inputs: dict[str, Any]) -> Any:
    from baskfy_core.ranking_engine import explain

    def run() -> Any:
        context = _context(inputs)
        return explain(context.result.scored.iloc[int(inputs["row_position"])], context)

    return _capture(run)


def call_explain_instrument(inputs: dict[str, Any]) -> Any:
    from baskfy_core.ranking_engine import explain_instrument

    return _capture(lambda: explain_instrument(int(inputs["instrument_id"]), _context(inputs)))


def _term(key: str, family: str = "momentum", preference: str = "higher", **kw: Any) -> dict[str, Any]:  # noqa: E501
    from baskfy_core.factor_registry import FACTORS

    factor = FACTORS.get(key)
    return {
        "key": key,
        "label": factor.label if factor else key.upper(),
        "weight_family": family,
        "preference": preference,
        "weight": kw.get("weight", 1.0),
        "target_min": kw.get("target_min"),
        "target_max": kw.get("target_max"),
        "null_policy": factor.null_policy.value if factor else kw.get("null_policy"),
    }


RANK_COLUMNS = [
    "instrument_id", "symbol", "passes_filters", "fail__series", "fail__min_return_1y", "sector",
    "ret_12m", "vol_12m", "ma_dist_50", "eff_ratio_63",
    "desk_score", "desk_score_rank", "desk_a_trend", "desk_b_momentum", "desk_c_sharpe",
    "desk_d_consistency", "desk_e_liquidity", "desk_f_penalty", "desk_ext_over_20dma",
    "desk_reject", "desk_score_version",
    "nse_mr6", "nse_mr12", "in_nifty_200", "is_fno",
    "last_bar_date", "recent_corporate_action",
]  # fmt: skip

# Ten names. Ties on ret_12m (14/15, 16/18), NULLs in every term, a filter failure (17), a NULL
# passes_filters (19), a desk reject (13), no desk row (18), a NULL sector (14, 20), a stale bar
# (16), a recent corporate action (12), instrument ids out of order so the tie-break is visible.
RANK_ROWS = [
    [15, "OMEGA", True, False, False, "nifty-it", 42.0, 0.31, 6.0, 0.41, 71.5, 3, 22.0, 21.0, 15.0, 8.0, 9.0, -3.5, 4.2, "", "desk-1", 1.30, 1.60, True, True, "2026-08-18", False],  # noqa: E501
    [11, "ALPHA", True, False, False, "nifty-bank", 55.5, 0.22, 21.0, 0.62, 84.0, 1, 24.0, 23.0, 18.0, 9.0, 10.0, 0.0, 9.1, "", "desk-1", 1.55, 1.90, True, True, "2026-08-18", False],  # noqa: E501
    [12, "BRAVO", True, False, False, "nifty-bank", 18.2, 0.40, -3.0, 0.15, 52.0, 6, 12.0, 14.0, 11.0, 6.0, 9.0, 0.0, -1.0, "", "desk-1", 1.05, 1.10, True, False, "2026-08-18", True],  # noqa: E501
    [13, "CHARLIE", True, False, False, "nifty-it", 30.0, 0.27, 12.0, None, None, None, 18.0, 16.0, 12.0, 7.0, 8.0, -10.0, 27.5, "extended;illiquid", "desk-1", 1.20, 1.40, True, True, "2026-08-18", False],  # noqa: E501
    [14, "DELTA", True, False, False, None, 42.0, None, 0.0, 0.55, 66.0, 4, 20.0, 19.0, 14.0, 7.0, 9.0, -2.0, 3.0, "", "desk-1", None, 1.20, True, True, "2026-08-18", False],  # noqa: E501
    [16, "ECHO", True, False, False, "nifty-pharma", 25.0, 0.35, 15.0, 0.33, 60.0, 5, 19.0, 18.0, 13.0, 6.0, 7.0, -1.0, 5.0, "", "desk-1", 0.95, 1.05, False, True, "2026-08-14", False],  # noqa: E501
    [17, "FOXTROT", False, True, True, "nifty-pharma", -5.0, 0.50, -12.0, 0.08, 40.0, 8, 8.0, 9.0, 10.0, 5.0, 8.0, 0.0, -8.0, "", "desk-1", 0.80, 0.90, True, True, "2026-08-18", False],  # noqa: E501
    [18, "GOLF", True, False, False, "nifty-it", 25.0, 0.29, 30.0, 0.47, None, None, None, None, None, None, None, None, None, None, None, 1.10, None, True, True, None, False],  # noqa: E501
    [19, "HOTEL", None, None, False, "nifty-bank", 61.0, 0.19, 8.0, 0.70, 77.0, 2, 23.0, 22.0, 17.0, 8.0, 10.0, 0.0, 6.0, "", "desk-1", 1.70, 2.10, True, True, "2026-08-18", False],  # noqa: E501
    [20, "INDIA", True, False, False, None, None, 0.26, None, 0.52, 58.0, 7, 15.0, 15.0, 12.0, 8.0, 8.0, 0.0, 2.0, "", "desk-1", 1.00, 1.25, True, True, "2026-08-18", False],  # noqa: E501
]
RANK_FRAME = {"columns": RANK_COLUMNS, "rows": RANK_ROWS, "date_columns": ["last_bar_date"]}


def _ranking_cases() -> list[tuple[str, str, dict[str, Any]]]:  # noqa: PLR0915 - a flat table
    r = "baskfy_core.ranking_engine."
    out: list[tuple[str, str, dict[str, Any]]] = []

    def add(fn: str, why: str, inputs: dict[str, Any]) -> None:
        out.append((r + fn, why, inputs))

    def spec(terms: list[dict[str, Any]], mode: str = "composite", scope: str = "filtered_results",
             missing: str = "penalize", weights: dict[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
        return {"terms": terms, "mode": mode, "scope": scope, "missing_data": missing,
                "family_weights": weights}  # fmt: skip

    mom = _term("ret_12m")
    mom2 = _term("sharpe_12m")
    mom3 = _term("rsi_12m", weight=2.0)
    path = _term("eff_ratio_63", "path_quality")
    risk = _term("vol_12m", "risk_execution", "lower")
    trend = _term("ma_dist_50", "trend_structure", "target_range", target_min=0.0, target_max=15.0)

    for fn in ("family_shares", "effective_weights"):
        for why, sp in (
            ("one family", spec([mom])),
            ("three momentum terms split momentum's share by weight", spec([mom, mom2, mom3])),
            ("four families, no weights: equal shares", spec([mom, path, risk, trend])),
            ("explicit momentum 60 / path 20; unset families take the mean of the explicit",
             spec([mom, mom2, path, risk, trend], weights={"momentum": 60, "path_quality": 20})),
            ("a weight for a family with no term is ignored",
             spec([mom, path], weights={"participation": 5, "momentum": 1})),
            ("every family used weighted zero is refused",
             spec([mom, path], weights={"momentum": 0, "path_quality": 0})),
            ("an unknown family is refused when the spec is built",
             spec([mom], weights={"vibes": 1})),
            ("a negative weight is refused", spec([mom], weights={"momentum": -1})),
        ):  # fmt: skip
            add(fn, why, {"spec": sp})

    for why, values in (
        ("empty", []),
        ("all missing", [None, None]),
        ("one value scores 1", [7.0]),
        ("one value among missing", [None, 7.0, None]),
        ("average ties", [10.0, 20.0, 20.0, 40.0]),
        ("missing values do not count toward n", [3.0, None, 1.0, 2.0, None]),
        ("negatives, zero and all-equal", [-1.5, 0.0, -1.5, 2.25]),
        ("all equal", [5.0, 5.0, 5.0]),
    ):
        add("percentile", why, {"values": values})

    for why, values, lower, upper in (
        ("both bounds", [-5.0, 0.0, 7.5, 15.0, 22.0, None], 0.0, 15.0),
        ("lower bound only", [-5.0, 0.0, 100.0, None], 0.0, None),
        ("upper bound only", [-5.0, 15.0, 16.5], None, 15.0),
        ("a point range", [2.0, 3.0, 4.0], 3.0, 3.0),
        ("empty", [], 0.0, 1.0),
    ):
        add("target_distance", why, {"values": values, "lower": lower, "upper": upper})

    for why, distances in (
        ("every row inside", [0.0, 0.0, 0.0]),
        ("inside scores 1 even when others are outside", [0.0, 2.0, 5.0, 0.0, 9.0]),
        ("all outside", [1.0, 3.0, 3.0]),
        ("missing stays missing", [None, 0.0, 4.0]),
        ("one outside alone", [6.0]),
    ):
        add("target_score", why, {"distances": distances})

    values = [10.0, 20.0, 20.0, None, 40.0]
    for why, term in (
        ("higher", _term("ret_12m")),
        ("lower", _term("vol_12m", "risk_execution", "lower")),
        ("target_range both bounds", _term("ma_dist_50", "trend_structure", "target_range",
                                           target_min=15.0, target_max=25.0)),
        ("target_range lower only", _term("ma_dist_50", "trend_structure", "target_range",
                                          target_min=30.0)),
        ("a target_range with no bound is refused", _term("ma_dist_50", "trend_structure",
                                                          "target_range")),
        ("an inverted target range is refused", _term("ma_dist_50", "trend_structure",
                                                      "target_range", target_min=5.0,
                                                      target_max=1.0)),
        ("a zero weight is refused", _term("ret_12m", weight=0.0)),
    ):  # fmt: skip
        add("transform", why, {"values": values, "term": term})

    nse_cols = ["instrument_id", "nse_mr6", "nse_mr12", "in_nifty_200", "is_fno"]
    nse_rows = [
        [1, 1.30, 1.60, True, True],
        [2, 1.55, 1.90, True, True],
        [3, 1.05, 1.10, True, False],
        [4, None, 1.20, True, True],
        [5, 0.80, 0.90, False, True],
        [6, 1.20, 1.40, True, True],
        [7, 0.95, 1.05, None, True],
    ]
    pop = ["nse_mr6_mean", "nse_mr6_std", "nse_mr12_mean", "nse_mr12_std"]
    for why, frame in (
        ("Z against the eligible rows present", {"columns": nse_cols, "rows": nse_rows}),
        ("Z against the stored population statistics",
         {"columns": [*nse_cols, *pop], "rows": [[*r, 1.1, 0.25, 1.3, 0.35] for r in nse_rows]}),
        ("population columns present but NULL fall back to the rows present",
         {"columns": [*nse_cols, *pop], "rows": [[*r, None, None, None, None] for r in nse_rows]}),
        ("one eligible name: zero std, every score NaN",
         {"columns": nse_cols, "rows": [nse_rows[0], nse_rows[2], nse_rows[4]]}),
        ("empty frame", {"columns": nse_cols, "rows": []}),
        ("is_fno missing is refused", {"columns": nse_cols[:-1], "rows": [r[:-1] for r in nse_rows]}),
    ):  # fmt: skip
        add("add_nse_momentum_score", why, {"frame": frame})

    desk = _term("desk_score")
    nse = _term("nse_momentum_score")
    for why, sp in (
        ("composite, filtered_results, penalize: ret/eff/vol/ma_dist across four families",
         spec([mom, path, risk, trend])),
        ("composite, fixed_universe: failing rows still count toward percentiles",
         spec([mom, path, risk, trend], scope="fixed_universe")),
        ("composite, within_sector: NULL sector is 'unclassified'",
         spec([mom, path], scope="within_sector")),
        ("composite, neutral missing data", spec([mom, path, risk], missing="neutral")),
        ("composite, exclude missing data", spec([mom, path, risk], missing="exclude")),
        ("composite with family weights", spec([mom, path, risk, trend],
                                               weights={"momentum": 3, "risk_execution": 1})),
        ("desk_score term: rejected and unscored rows are never ranked",
         spec([desk, mom], weights={"momentum": 1})),
        ("nse_momentum_score is computed when absent from the frame", spec([nse, mom])),
        ("sequential: raw values in term order, missing last, ties by instrument_id",
         spec([mom, path], mode="sequential")),
        ("single: one term, lower is better", spec([risk], mode="single")),
        ("single target_range: distance orders", spec([trend], mode="single")),
    ):  # fmt: skip
        add("rank_frame", why, {"frame": RANK_FRAME, "spec": sp})
    add("rank_frame", "an empty frame ranks nothing",
        {"frame": {"columns": RANK_COLUMNS, "rows": []}, "spec": spec([mom])})
    add("rank_frame", "duplicate instrument_id is refused",
        {"frame": {**RANK_FRAME, "rows": [RANK_ROWS[0], RANK_ROWS[0]]}, "spec": spec([mom])})
    add("rank_frame", "a frame without passes_filters is refused",
        {"frame": {"columns": ["instrument_id", "ret_12m"], "rows": [[1, 2.0]]},
         "spec": spec([mom])})  # fmt: skip
    add("rank_frame", "a term column missing from the frame is refused",
        {"frame": RANK_FRAME, "spec": spec([_term("sortino_12m")])})

    for why, today, previous, prev_as_of in (
        ("gained places", 3, 7, "2026-08-17"),
        ("lost places", 9, 2, "2026-08-17"),
        ("unchanged", 4, 4, "2026-08-17"),
        ("new today: no previous rank", 5, None, "2026-08-17"),
        ("not ranked today", None, 6, "2026-08-17"),
        ("no previous session", 1, None, None),
    ):
        add("rank_history", why, {"today": today, "previous": previous,
                                  "previous_as_of": prev_as_of})  # fmt: skip

    context = {
        "universe": "nifty-500",
        "as_of": AS_OF.isoformat(),
        "data_version": 12,
        "desk_score_version": "desk-1",
        "clause_details": {"series": "Series in EQ", "min_return_1y": "1Y return >= 0"},
        "previous_as_of": "2026-08-14",
        "previous_rank": 4,
    }
    composite = spec([mom, path, risk, trend])
    for why, position, sp, ctx in (
        ("OMEGA: ranked, tied on ret_12m, inside the target range", 0, composite, context),
        ("ALPHA: top on every term, desk components above 80%", 1, composite, context),
        ("BRAVO: recent corporate action, outside the target range", 2, composite, context),
        ("CHARLIE: desk reject tokens and the F floor", 3, spec([desk, mom]), context),
        ("DELTA: missing vol_12m under penalize, NULL sector", 4, composite, context),
        ("ECHO: stale last bar", 5, composite, context),
        ("FOXTROT: fails two clauses, details from the query builder", 6, composite, context),
        ("GOLF: no desk row and no last bar", 7, spec([desk, mom]), context),
        ("HOTEL: NULL passes_filters is not a pass", 8, composite, context),
        ("INDIA: excluded for missing data", 9, spec([mom, path], missing="exclude"), context),
        ("within_sector: the notes cite the sector", 1,
         spec([mom, path], scope="within_sector"), context),
        ("sequential: no total, no effective weights, no previous rank", 1,
         spec([mom, path], mode="sequential"), {**context, "previous_rank": None}),
        ("fixed_universe phrase, no clause details, no previous session", 2,
         spec([mom, path], scope="fixed_universe"),
         {"universe": "nifty-200", "as_of": AS_OF.isoformat()}),
    ):  # fmt: skip
        add("explain", why, {"frame": RANK_FRAME, "spec": sp, "context": ctx,
                             "row_position": position})  # fmt: skip

    for why, iid, sp in (
        ("by instrument id", 11, composite),
        ("a within_sector row", 14, spec([mom, path], scope="within_sector")),
        ("an unranked row", 17, composite),
        ("an id not in the frame raises", 999, composite),
    ):
        add("explain_instrument", why, {"frame": RANK_FRAME, "spec": sp, "context": context,
                                        "instrument_id": iid})  # fmt: skip
    return out


# ---------------------------------------------------------------------------
# The table and the writer
# ---------------------------------------------------------------------------

#: Measured while writing this lane (29 Oct 2026), not fixed here: the dumper never modifies the
#: Python. ``classify_regimes`` labels only the frame's latest date, over each instrument's whole
#: history, so bars after ``as_of`` make every ``as_of`` regime NULL instead of being ignored.
LOOKAHEAD_REGIME_BUG = (
    "compute_factors is not self-bounding at as_of for the regime columns: "
    "instrument_regime.classify_regimes classifies each instrument's whole history and labels "
    "only the frame's max date, so with bars after as_of the as_of row's regime, "
    "regime_distance_bull and regime_distance_bear are NULL. With the same bars truncated at "
    "as_of they are NEUTRAL / 0.0032306171918009426 / 0.003137655413468552 (unrounded); every "
    "other column is identical. Callers must pass bars <= as_of (house rule 5). This golden "
    "records the current output."
)

CALLS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "baskfy_core.factors.compute_factors": call_compute_factors,
    "baskfy_core.factors.compute_factors_unrounded": call_compute_factors_unrounded,
    "baskfy_core.factor_registry.FACTORS": call_registry_table,
    "baskfy_core.factor_registry.columns": call_columns,
    "baskfy_core.factor_registry.by_family": call_by_family,
    "baskfy_core.factor_registry.family_counts": call_family_counts,
    "baskfy_core.factor_registry.get": call_get,
    "baskfy_core.factor_registry.sql_for": call_sql_for,
    "baskfy_core.screener.validate_definition": call_validate_definition,
    "baskfy_core.screener.resolve_columns": call_resolve_columns,
    "baskfy_core.screener.build_screen_query": call_build_screen_query,
    "baskfy_core.screener.build_survivors_query": call_build_survivors_query,
    "baskfy_core.screener.build_ranking_frame_query": call_build_ranking_frame_query,
    "baskfy_core.screener.build_export_query": call_build_export_query,
    "baskfy_core.screener.cache_key": call_cache_key,
    "baskfy_core.screener.canonical_json": call_canonical_json,
    "baskfy_core.screener.ScreenResult.to_json": call_screen_result_to_json,
    "baskfy_core.ranking_engine.family_shares": call_family_shares,
    "baskfy_core.ranking_engine.effective_weights": call_effective_weights,
    "baskfy_core.ranking_engine.percentile": call_percentile,
    "baskfy_core.ranking_engine.target_distance": call_target_distance,
    "baskfy_core.ranking_engine.target_score": call_target_score,
    "baskfy_core.ranking_engine.transform": call_transform,
    "baskfy_core.ranking_engine.add_nse_momentum_score": call_add_nse_momentum_score,
    "baskfy_core.ranking_engine.rank_frame": call_rank_frame,
    "baskfy_core.ranking_engine.rank_history": call_rank_history,
    "baskfy_core.ranking_engine.explain": call_explain,
    "baskfy_core.ranking_engine.explain_instrument": call_explain_instrument,
}


def cases() -> list[tuple[str, str, str, dict[str, Any], str | None]]:
    """``(fn, case file stem, why, JSON inputs, known_bug)`` for every golden, in a fixed order."""
    out: list[tuple[str, str, str, dict[str, Any], str | None]] = []

    def add(fn: str, why: str, inputs: dict[str, Any]) -> None:
        assert fn in CALLS, fn
        short = fn.removeprefix("baskfy_core.")
        n = sum(1 for f, *_ in out if f == fn) + 1
        bug = LOOKAHEAD_REGIME_BUG if "no look-ahead" in why else None
        out.append((fn, f"{short}.case_{n:03d}", why, inputs, bug))

    factor_cases = _factor_cases()
    for why, inputs in factor_cases:
        add("baskfy_core.factors.compute_factors", why, inputs)
    for position in (0, 2, 4):  # full, adjusted-vs-raw, look-ahead
        why, inputs = factor_cases[position]
        add("baskfy_core.factors.compute_factors_unrounded", f"unrounded: {why}", inputs)

    registry = "baskfy_core.factor_registry."
    add(registry + "FACTORS", "the whole registry and its derived key lists", {})
    add(registry + "columns", "docs/01 §4's column picker, in order", {})
    add(registry + "by_family", "factor keys grouped by family, every family present", {})
    add(registry + "family_counts", "factor count per family", {})
    for key in REGISTRY_KEYS:
        add(registry + "get", f"get({key!r})", {"key": key})
    for key in REGISTRY_KEYS:
        add(registry + "sql_for", f"sql_for({key!r})", {"key": key})

    for fn, why, inputs in _screener_cases():
        add(fn, why, inputs)
    for fn, why, inputs in _ranking_cases():
        add(fn, why, inputs)
    return out


def render_case(fn: str, why: str, inputs: dict[str, Any], known_bug: str | None = None) -> str:
    golden = _golden()
    # Round-trip the inputs through their JSON text first, so the stored output is the output of
    # exactly what the file says the inputs are.
    stored = json.loads(golden.render(golden._enc(inputs)))
    doc = golden.document(
        fn=fn,
        inputs=stored,
        output=CALLS[fn](stored),
        known_bug=known_bug,
        notes=f"{why}. {NOTES}",
        stable=True,
    )
    return golden.render(doc)


def dump_ranking() -> list[Path]:
    golden = _golden()
    directory = golden.GOLDEN_DIR / LANE
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for fn, stem, why, inputs, known_bug in cases():
        path = directory / f"{stem}.json"
        path.write_text(render_case(fn, why, inputs, known_bug), encoding="utf-8")
        written.append(path)
    return written


if __name__ == "__main__":
    paths = dump_ranking()
    print(f"{len(paths)} ranking goldens written under go/testdata/golden/{LANE}/")
