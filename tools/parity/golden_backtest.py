"""The backtest lane of the Rust/Go parity goldens (the rewrite's answer key, 29 Oct 2026).

    cd decile-blueprint
    .venv/bin/python ../tools/parity/golden_backtest.py

Dumps the public surface of ``baskfy_core.backtest`` (the point-in-time engine, docs/10) and
``baskfy_core.backtest_metrics`` into ``go/testdata/golden/L1/backtest/<function>.case_NNN.json``,
written ``stable=True`` by ``tools/parity/golden.py``'s own encoder, so two runs are byte-identical.

Like the options lane, every case's inputs are stored as JSON first and each function is called
**from those stored inputs** (decoded back into frames, configs and results here), never from
Python objects that were not written down. A port reads ``inputs``, decodes them by the same
rules, and must reproduce ``output``.

Fixtures are synthetic and deterministic: a seeded ``numpy`` generator, a weekday calendar from
2024-01-01 with two fixed exchange holidays, prices on a two-decimal grid (which sits on the
engine's four-decimal ``PRICE_EXPONENT`` grid exactly). No network, no database, no clock.

Encoding conventions (restated in every case's ``meta.notes``)
--------------------------------------------------------------
* Decimals are strings; dates are ISO days; enums are their values.
* A polars frame is ``{"columns", "dtypes", "rows"}`` (golden.py's native polars encoding).
* ``data`` (a ``BacktestData``) is ``{"calendar", "prices", "price_only", "screens": {date:
  frame}, "benchmark", "delistings": {id: date}, "symbols", "names", "dividends",
  "data_version", "adjustment_coverage"}``. ``prices`` carries ``open``/``close`` (the adjusted
  series, house rule 6) and, where a case needs it, ``price_open``/``price_close`` (the raw print);
  ``price_only=true`` makes the panel read the raw pair.
* ``config`` is ``BacktestConfig.model_dump(mode="json")`` — every default written out.
* A raised engine error is ``{"error": {"type", "message"}}`` (``LookAheadError`` adds ``what``,
  ``offending``, ``as_of``). That *is* the expected output for that case.
* ``compute_metrics``'s output is ``Metrics.as_dict()`` — the published (rounded) contract — and
  ``run_backtest``/``run_fragility`` outputs are the result dataclasses encoded field for field.
* House rule 5 (no look-ahead): every screen frame carries ``date`` = its rebalance day, and the
  ranking in it is computed from closes on or before that day only. The look-ahead cases inject a
  row dated after the cursor and record the refusal.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import importlib.util
import sys
from collections.abc import Callable, Iterator
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


LANE = "L1/backtest"
NOTES = (
    "Decimals are strings; dates are ISO days; enums are their values; polars frames are "
    "{columns, dtypes, rows}. Prices: open/close are the adjusted series (house rule 6), "
    "price_open/price_close the raw print, read only when price_only is true. Config is "
    "BacktestConfig.model_dump(mode='json'). A raised engine error is {error: {type, message}}. "
    "Synthetic seeded fixtures (weekday calendar from 2024-01-01, holidays 2024-01-26 and "
    "2024-03-25). Regenerate with `.venv/bin/python ../tools/parity/golden_backtest.py` from "
    "decile-blueprint; each case is computed from its stored inputs."
)

D = Decimal


def _enc(obj: Any) -> Any:
    return _golden()._enc(obj)


# =============================================================================================
# Fixtures — deterministic, synthetic
# =============================================================================================

HOLIDAYS = (dt.date(2024, 1, 26), dt.date(2024, 3, 25))


def calendar(start: dt.date, count: int, holidays: tuple[dt.date, ...] = HOLIDAYS) -> list[dt.date]:
    days: list[dt.date] = []
    day = start
    while len(days) < count:
        if day.weekday() < 5 and day not in holidays:
            days.append(day)
        day += dt.timedelta(days=1)
    return days


CAL = calendar(dt.date(2024, 1, 1), 140)  # 2024-01-01 .. ~2024-07-17
IDS = (101, 102, 103, 104, 105, 106, 107, 108)
SYMBOLS = {i: s for i, s in zip(IDS, ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"), strict=True)}
NAMES = {i: f"{s} Industries Ltd" for i, s in SYMBOLS.items()}
SHARES_CR = {101: 12.0, 102: 4.5, 103: 30.0, 104: 8.0, 105: 2.25, 106: 15.0, 107: 6.0, 108: 1.0}
VOL = {101: 0.18, 102: 0.32, 103: 0.22, 104: 0.41, 105: 0.27, 106: 0.15, 107: 0.36, 108: 0.29}


def _walks(cal: list[dt.date], seed: int = 20261029) -> dict[int, tuple[list[float], list[float]]]:
    """(opens, closes) per instrument on ``cal``. Two-decimal grid, seeded."""
    rng = np.random.default_rng(seed)
    out: dict[int, tuple[list[float], list[float]]] = {}
    for k, inst in enumerate(IDS):
        drift = (k - 3.5) * 0.0012
        vol = 0.012 + 0.003 * (k % 4)
        level = 100.0 + 37.0 * k
        opens: list[float] = []
        closes: list[float] = []
        for _ in cal:
            gap = float(rng.normal(0.0, 0.004))
            move = float(rng.normal(drift, vol))
            o = round(level * (1.0 + gap), 2)
            c = round(o * (1.0 + move), 2)
            opens.append(o)
            closes.append(c)
            level = c
        out[inst] = (opens, closes)
    return out


WALKS = _walks(CAL)


def price_frame(
    cal: list[dt.date] = CAL,
    walks: dict[int, tuple[list[float], list[float]]] = WALKS,
    *,
    drop: set[tuple[int, dt.date]] | None = None,
    null_open: set[tuple[int, dt.date]] | None = None,
    stop_after: dict[int, dt.date] | None = None,
    raw_split: tuple[int, dt.date, float] | None = None,
) -> pl.DataFrame:
    """Long-form bars. ``drop`` removes a bar; ``null_open`` keeps the close and nulls the open;
    ``stop_after`` ends a series; ``raw_split`` = (id, ex_date, ratio) makes the *adjusted* series
    continuous and the raw ``price_*`` series jump by ``ratio`` before ``ex_date``."""
    drop = drop or set()
    null_open = null_open or set()
    stop_after = stop_after or {}
    rows: list[dict[str, Any]] = []
    for inst in IDS:
        opens, closes = walks[inst]
        for day, o, c in zip(cal, opens, closes, strict=True):
            if (inst, day) in drop or (inst in stop_after and day > stop_after[inst]):
                continue
            raw_mult = 1.0
            if raw_split is not None and raw_split[0] == inst and day < raw_split[1]:
                raw_mult = raw_split[2]
            rows.append(
                {
                    "date": day,
                    "instrument_id": inst,
                    "open": None if (inst, day) in null_open else o,
                    "close": c,
                    "price_open": None if (inst, day) in null_open else round(o * raw_mult, 2),
                    "price_close": round(c * raw_mult, 2),
                }
            )
    return pl.DataFrame(
        rows,
        schema={
            "date": pl.Date,
            "instrument_id": pl.Int64,
            "open": pl.Float64,
            "close": pl.Float64,
            "price_open": pl.Float64,
            "price_close": pl.Float64,
        },
    )


def screen_frame(
    day: dt.date,
    cal: list[dt.date] = CAL,
    walks: dict[int, tuple[list[float], list[float]]] = WALKS,
    *,
    lookback: int = 20,
    exclude: frozenset[int] = frozenset(),
    null_marketcap: frozenset[int] = frozenset(),
    stamp: dt.date | None = None,
    tie: tuple[int, int] | None = None,
) -> pl.DataFrame:
    """The screen as of ``day``: rank by trailing ``lookback``-day return, closes <= ``day`` only
    (house rule 5). ``stamp`` overrides the ``date`` column (the look-ahead injection)."""
    position = cal.index(day)
    base = max(0, position - lookback)
    scored: list[tuple[float, int]] = []
    for inst in IDS:
        if inst in exclude:
            continue
        closes = walks[inst][1]
        scored.append((closes[position] / closes[base] - 1.0, inst))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    rows = []
    for rank, (_, inst) in enumerate(scored, start=1):
        close = walks[inst][1][position]
        rows.append(
            {
                "date": stamp or day,
                "instrument_id": inst,
                "symbol": SYMBOLS[inst],
                "name": NAMES[inst],
                "rank": rank,
                "marketcap_cr": None if inst in null_marketcap else round(SHARES_CR[inst] * close, 2),
                "vol_12m": VOL[inst],
            }
        )
    if tie is not None:  # two names share a rank
        a, b = tie
        ra = next(r["rank"] for r in rows if r["instrument_id"] == a)
        for r in rows:
            if r["instrument_id"] == b:
                r["rank"] = ra
    return pl.DataFrame(
        rows,
        schema={
            "date": pl.Date,
            "instrument_id": pl.Int64,
            "symbol": pl.String,
            "name": pl.String,
            "rank": pl.Int64,
            "marketcap_cr": pl.Float64,
            "vol_12m": pl.Float64,
        },
    )


def benchmark_frame(
    cal: list[dt.date], *, start: float = 20000.0, drift: float = 0.0006, seed: int = 7,
    nulls: frozenset[dt.date] = frozenset(), crash_from: int | None = None,
) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    level = start
    rows = []
    for index, day in enumerate(cal):
        mu = -0.02 if crash_from is not None and index >= crash_from else drift
        level = round(level * (1.0 + float(rng.normal(mu, 0.008))), 2)
        rows.append({"date": day, "level": None if day in nulls else level})
    return pl.DataFrame(rows, schema={"date": pl.Date, "level": pl.Float64})


def data_json(  # noqa: PLR0913 - one keyword per BacktestData field
    *,
    cal: list[dt.date] = CAL,
    prices: pl.DataFrame | None = None,
    price_only: bool = False,
    screens: dict[dt.date, pl.DataFrame],
    benchmark: pl.DataFrame | None = None,
    delistings: dict[int, dt.date] | None = None,
    dividends: pl.DataFrame | None = None,
    data_version: int | None = 42,
    adjustment_coverage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _enc(
        {
            "calendar": cal,
            "prices": prices if prices is not None else price_frame(cal),
            "price_only": price_only,
            "screens": {day.isoformat(): frame for day, frame in screens.items()},
            "benchmark": benchmark,
            "delistings": {str(k): v for k, v in (delistings or {}).items()},
            "symbols": {str(k): v for k, v in SYMBOLS.items()},
            "names": {str(k): v for k, v in NAMES.items()},
            "dividends": dividends,
            "data_version": data_version,
            "adjustment_coverage": adjustment_coverage,
        }
    )


def config_json(**kw: Any) -> dict[str, Any]:
    from baskfy_core.backtest import BacktestConfig

    return BacktestConfig.model_validate(kw).model_dump(mode="json")


# =============================================================================================
# Decoders — JSON inputs back into engine objects
# =============================================================================================


def _date(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


def _dates(values: list[str]) -> tuple[dt.date, ...]:
    return tuple(_date(v) for v in values)


def _dec(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def frame_from(doc: dict[str, Any] | None) -> pl.DataFrame | None:
    return None if doc is None else _golden().polars_frame_from(doc)


def decode_config(doc: dict[str, Any]) -> Any:
    from baskfy_core.backtest import BacktestConfig

    return BacktestConfig.model_validate(doc)


def decode_coverage(doc: dict[str, Any] | None) -> Any:
    from baskfy_core.backtest import AdjustmentCoverage

    if doc is None:
        return None
    return AdjustmentCoverage(
        actions_from=None if doc["actions_from"] is None else _date(doc["actions_from"]),
        actions_to=None if doc["actions_to"] is None else _date(doc["actions_to"]),
        self_adjusted_from=(
            None if doc["self_adjusted_from"] is None else _date(doc["self_adjusted_from"])
        ),
        splice_on=None if doc["splice_on"] is None else _date(doc["splice_on"]),
        as_of_today=bool(doc["as_of_today"]),
    )


def decode_data(doc: dict[str, Any]) -> Any:
    from baskfy_core.backtest import BacktestData, PricePanel

    cal = _dates(doc["calendar"])
    prices = frame_from(doc["prices"])
    assert prices is not None
    return BacktestData(
        calendar=cal,
        prices=PricePanel(prices, cal, price_only=bool(doc["price_only"])),
        screens={_date(k): frame_from(v) for k, v in doc["screens"].items()},
        benchmark=frame_from(doc["benchmark"]),
        delistings={int(k): _date(v) for k, v in doc["delistings"].items()},
        symbols={int(k): v for k, v in doc["symbols"].items()},
        names={int(k): v for k, v in doc["names"].items()},
        dividends=frame_from(doc["dividends"]),
        data_version=doc["data_version"],
        adjustment_coverage=decode_coverage(doc["adjustment_coverage"]),
    )


def decode_trade(doc: dict[str, Any]) -> Any:
    from baskfy_core.backtest import Trade, TradeReason, TradeSide

    return Trade(
        date=_date(doc["date"]),
        instrument_id=int(doc["instrument_id"]),
        symbol=doc["symbol"],
        side=TradeSide(doc["side"]),
        quantity=int(doc["quantity"]),
        price=Decimal(doc["price"]),
        notional=Decimal(doc["notional"]),
        cost=Decimal(doc["cost"]),
        reason=TradeReason(doc["reason"]),
        realised_pnl=_dec(doc["realised_pnl"]),
    )


def decode_result(doc: dict[str, Any]) -> Any:
    from baskfy_core.backtest import BacktestResult, DelistingEvent, HoldingSnapshot

    return BacktestResult(
        config=decode_config(doc["config"]),
        dates=_dates(doc["dates"]),
        equity=tuple(Decimal(v) for v in doc["equity"]),
        cash=tuple(Decimal(v) for v in doc["cash"]),
        invested=tuple(Decimal(v) for v in doc["invested"]),
        benchmark=tuple(_dec(v) for v in doc["benchmark"]),
        trades=tuple(decode_trade(t) for t in doc["trades"]),
        holdings=tuple(
            HoldingSnapshot(
                rebalance_date=_date(h["rebalance_date"]),
                executed_on=_date(h["executed_on"]),
                instrument_id=int(h["instrument_id"]),
                symbol=h["symbol"],
                name=h["name"],
                rank=h["rank"],
                target_weight=Decimal(h["target_weight"]),
                quantity=int(h["quantity"]),
                price=Decimal(h["price"]),
                value=Decimal(h["value"]),
                actual_weight=Decimal(h["actual_weight"]),
            )
            for h in doc["holdings"]
        ),
        delistings=tuple(
            DelistingEvent(
                date=_date(e["date"]),
                instrument_id=int(e["instrument_id"]),
                symbol=e["symbol"],
                quantity=int(e["quantity"]),
                price=Decimal(e["price"]),
                priced_on=_date(e["priced_on"]),
                proceeds=Decimal(e["proceeds"]),
            )
            for e in doc["delistings"]
        ),
        total_costs=Decimal(doc["total_costs"]),
        dividends_credited=Decimal(doc["dividends_credited"]),
        rebalance_dates=_dates(doc["rebalance_dates"]),
        blind_rebalances=_dates(doc["blind_rebalances"]),
        adjustment_coverage=decode_coverage(doc["adjustment_coverage"]),
        unexecuted_fills=_dates(doc["unexecuted_fills"]),
        notes=tuple(doc["notes"]),
        data_version=doc["data_version"],
    )


def _error(exc: Exception) -> dict[str, Any]:
    from baskfy_core.backtest import LookAheadError

    body: dict[str, Any] = {"type": type(exc).__name__, "message": str(exc)}
    if isinstance(exc, LookAheadError):
        body.update({"what": exc.what, "offending": exc.offending, "as_of": exc.as_of})
    return {"error": body}


def _guarded(fn: Callable[[], Any]) -> Any:
    from baskfy_core.backtest import BacktestError

    try:
        return fn()
    except BacktestError as exc:
        return _error(exc)


# =============================================================================================
# Calls — stored inputs -> the output the golden stores
# =============================================================================================


def call_rebalance_dates(inputs: dict[str, Any]) -> Any:
    from baskfy_core.backtest import RebalanceSpec, rebalance_dates

    return rebalance_dates(
        _dates(inputs["calendar"]), _date(inputs["start"]), _date(inputs["end"]),
        RebalanceSpec.model_validate(inputs["spec"]),
    )  # fmt: skip


def call_shift_schedule(inputs: dict[str, Any]) -> Any:
    from baskfy_core.backtest import shift_schedule

    return shift_schedule(_dates(inputs["calendar"]), _dates(inputs["dates"]), int(inputs["offset"]))


def call_apply_position_limits(inputs: dict[str, Any]) -> Any:
    from baskfy_core.backtest import PositionLimits, apply_position_limits

    weights = {int(k): Decimal(v) for k, v in inputs["weights"].items()}
    out = apply_position_limits(weights, PositionLimits.model_validate(inputs["limits"]))
    return {"weights": {str(k): v for k, v in out.items()}, "sum": sum(out.values(), D(0))}


def call_raw_weights(inputs: dict[str, Any]) -> Any:
    from baskfy_core.backtest import Weighting, _Selected, raw_weights

    rows = [
        _Selected(
            instrument_id=int(r["instrument_id"]), symbol=r["symbol"], name=r["name"],
            rank=int(r["rank"]), marketcap_cr=_dec(r["marketcap_cr"]),
            volatility=_dec(r["volatility"]),
        )
        for r in inputs["rows"]
    ]  # fmt: skip
    return _guarded(
        lambda: {str(k): v for k, v in raw_weights(Weighting(inputs["scheme"]), rows).items()}
    )


def call_cost_charge(inputs: dict[str, Any]) -> Any:
    from baskfy_core.backtest import CostSpec

    costs = CostSpec.model_validate(inputs["costs"])
    if inputs.get("scale") is not None:
        costs = costs.scaled(Decimal(inputs["scale"]))
    return {
        "total_bps": costs.total_bps,
        "charges": [costs.charge(Decimal(n)) for n in inputs["notionals"]],
    }


def call_caveats(inputs: dict[str, Any]) -> Any:
    coverage = decode_coverage(inputs["coverage"])
    return list(coverage.caveats(_date(inputs["start"]), _date(inputs["end"])))


def call_year_fraction(inputs: dict[str, Any]) -> Any:
    from baskfy_core.backtest import year_fraction

    return year_fraction(_date(inputs["start"]), _date(inputs["end"]))


def call_month_key(inputs: dict[str, Any]) -> Any:
    from baskfy_core.backtest import month_key

    return month_key(_date(inputs["day"]))


def call_month_end(inputs: dict[str, Any]) -> Any:
    from baskfy_core.backtest import month_end

    return month_end(_date(inputs["day"]))


def call_price_panel(inputs: dict[str, Any]) -> Any:
    """``PricePanel``'s public reads, as a list of ops over one panel."""
    from baskfy_core.backtest import PricePanel

    cal = _dates(inputs["calendar"])
    frame = frame_from(inputs["prices"])
    assert frame is not None

    def build() -> Any:
        return PricePanel(frame, cal, price_only=bool(inputs["price_only"]))

    built = _guarded(build)
    if isinstance(built, dict):
        return built
    panel = built
    results: list[Any] = []
    for op in inputs["ops"]:
        kind = op["op"]
        if kind == "index_of":
            results.append(_guarded(lambda op=op: panel.index_of(_date(op["day"]))))
        elif kind == "close_on":
            results.append(panel.close_on(int(op["instrument_id"]), int(op["position"])))
        elif kind == "open_on":
            results.append(panel.open_on(int(op["instrument_id"]), int(op["position"])))
        elif kind == "last_close_at_or_before":
            found = panel.last_close_at_or_before(int(op["instrument_id"]), int(op["position"]))
            results.append(None if found is None else {"price": found[0], "position": found[1]})
        elif kind == "liquidation_day":
            delisted = op["delisted_on"]
            results.append(
                panel.liquidation_day(
                    int(op["instrument_id"]), None if delisted is None else _date(delisted)
                )
            )
        else:  # pragma: no cover - a fixture typo
            raise ValueError(kind)
    return {"instrument_ids": list(panel.instrument_ids), "results": results}


def call_point_in_time_reader(inputs: dict[str, Any]) -> Any:
    """The look-ahead guard (house rule 5) as a sequence of ops; each op's result or refusal."""
    from baskfy_core.backtest import BacktestError, PointInTimeReader

    reader = PointInTimeReader(decode_data(inputs["data"]))
    results: list[Any] = []
    for op in inputs["ops"]:
        kind = op["op"]
        try:
            if kind == "as_of":
                results.append(reader.as_of)
            elif kind == "advance_to":
                reader.advance_to(_date(op["day"]))
                results.append(None)
            elif kind == "screen_on":
                results.append(reader.screen_on(_date(op["day"])))
            elif kind == "benchmark_to":
                results.append(reader.benchmark_to(_date(op["day"])))
            elif kind == "dividends_on":
                results.append(reader.dividends_on(_date(op["day"])))
            elif kind == "price_close":
                results.append(reader.price_close(int(op["instrument_id"]), _date(op["day"])))
            elif kind == "price_open":
                results.append(reader.price_open(int(op["instrument_id"]), _date(op["day"])))
            elif kind == "last_close":
                found = reader.last_close(int(op["instrument_id"]), _date(op["day"]))
                results.append(None if found is None else {"price": found[0], "date": found[1]})
            else:  # pragma: no cover
                raise ValueError(kind)
        except BacktestError as exc:
            results.append(_error(exc))
    return results


def call_run_backtest(inputs: dict[str, Any]) -> Any:
    from baskfy_core.backtest import run_backtest

    schedule = inputs.get("schedule")
    return _guarded(
        lambda: run_backtest(
            decode_config(inputs["config"]), decode_data(inputs["data"]),
            schedule=None if schedule is None else _dates(schedule),
        )
    )  # fmt: skip


def call_run_fragility(inputs: dict[str, Any]) -> Any:
    from baskfy_core.backtest import run_fragility

    return _guarded(lambda: run_fragility(decode_config(inputs["config"]), decode_data(inputs["data"])))


def call_drawdown_series(inputs: dict[str, Any]) -> Any:
    from baskfy_core.backtest_metrics import drawdown_series

    return drawdown_series(_dates(inputs["dates"]), [Decimal(v) for v in inputs["equity"]])


def call_monthly_returns(inputs: dict[str, Any]) -> Any:
    from baskfy_core.backtest_metrics import monthly_returns

    rows = monthly_returns(_dates(inputs["dates"]), [Decimal(v) for v in inputs["equity"]])
    return [{"year": r.year, "month": r.month, "key": r.key, "ret": r.ret} for r in rows]


def call_downsample_curve(inputs: dict[str, Any]) -> Any:
    from baskfy_core.backtest_metrics import downsample_curve

    kw: dict[str, Any] = {}
    if inputs.get("limit") is not None:
        kw["limit"] = int(inputs["limit"])
    return downsample_curve(
        _dates(inputs["dates"]), [Decimal(v) for v in inputs["equity"]],
        [_dec(v) for v in inputs["benchmark"]], **kw,
    )  # fmt: skip


def call_round_trips(inputs: dict[str, Any]) -> Any:
    from baskfy_core.backtest_metrics import round_trips

    trips = round_trips([decode_trade(t) for t in inputs["trades"]])
    return [{**dataclasses.asdict(t), "is_win": t.is_win} for t in trips]


def call_rolling_distribution(inputs: dict[str, Any]) -> Any:
    from baskfy_core.backtest_metrics import rolling_distribution

    dist = rolling_distribution(_dates(inputs["dates"]), [Decimal(v) for v in inputs["equity"]])
    return {"raw": dist, "as_dict": dist.as_dict()}


def call_compute_metrics(inputs: dict[str, Any]) -> Any:
    from baskfy_core.backtest_metrics import compute_metrics

    return compute_metrics(decode_result(inputs["result"])).as_dict()


def call_metrics_hash(inputs: dict[str, Any]) -> Any:
    from baskfy_core.backtest_metrics import compute_metrics, metrics_hash

    result = decode_result(inputs["result"])
    metrics = compute_metrics(result)
    return {
        "metrics": metrics.as_dict(),
        "hash": metrics_hash(metrics, result.config, inputs["data_version"]),
    }


CALLS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "baskfy_core.backtest.rebalance_dates": call_rebalance_dates,
    "baskfy_core.backtest.shift_schedule": call_shift_schedule,
    "baskfy_core.backtest.apply_position_limits": call_apply_position_limits,
    "baskfy_core.backtest.raw_weights": call_raw_weights,
    "baskfy_core.backtest.CostSpec.charge": call_cost_charge,
    "baskfy_core.backtest.AdjustmentCoverage.caveats": call_caveats,
    "baskfy_core.backtest.year_fraction": call_year_fraction,
    "baskfy_core.backtest.month_key": call_month_key,
    "baskfy_core.backtest.month_end": call_month_end,
    "baskfy_core.backtest.PricePanel": call_price_panel,
    "baskfy_core.backtest.PointInTimeReader": call_point_in_time_reader,
    "baskfy_core.backtest.run_backtest": call_run_backtest,
    "baskfy_core.backtest.run_fragility": call_run_fragility,
    "baskfy_core.backtest_metrics.drawdown_series": call_drawdown_series,
    "baskfy_core.backtest_metrics.monthly_returns": call_monthly_returns,
    "baskfy_core.backtest_metrics.downsample_curve": call_downsample_curve,
    "baskfy_core.backtest_metrics.round_trips": call_round_trips,
    "baskfy_core.backtest_metrics.rolling_distribution": call_rolling_distribution,
    "baskfy_core.backtest_metrics.compute_metrics": call_compute_metrics,
    "baskfy_core.backtest_metrics.metrics_hash": call_metrics_hash,
}


# =============================================================================================
# Cases
# =============================================================================================


def _screens_for(days: list[dt.date], **kw: Any) -> dict[dt.date, pl.DataFrame]:
    return {day: screen_frame(day, **kw) for day in days}


def _rebal(start: dt.date, end: dt.date, **spec: Any) -> list[dt.date]:
    from baskfy_core.backtest import RebalanceSpec, rebalance_dates

    return list(rebalance_dates(CAL, start, end, RebalanceSpec.model_validate(spec)))


def _trade(day: str, inst: int, side: str, qty: int, price: str, cost: str, reason: str,
           pnl: str | None = None) -> dict[str, Any]:  # fmt: skip
    notional = (D(price) * qty).quantize(D("0.01"))
    return {"date": day, "instrument_id": inst, "symbol": SYMBOLS.get(inst, f"#{inst}"),
            "side": side, "quantity": qty, "price": price, "notional": str(notional),
            "cost": cost, "reason": reason, "realised_pnl": pnl}  # fmt: skip


def _equity_path(n: int, seed: int, *, start: float = 1_000_000.0, drift: float = 0.0004,
                 vol: float = 0.01) -> list[str]:  # fmt: skip
    rng = np.random.default_rng(seed)
    level = start
    out = []
    for _ in range(n):
        out.append(str(D(repr(level)).quantize(D("0.01"))))
        level = level * (1.0 + float(rng.normal(drift, vol)))
    return out


def _synthetic_result(  # noqa: PLR0913
    dates: list[dt.date], equity: list[str], *, invested: list[str] | None = None,
    benchmark: list[str | None] | None = None, trades: list[dict[str, Any]] | None = None,
    config: dict[str, Any] | None = None, total_costs: str = "0", delistings: int = 0,
) -> dict[str, Any]:  # fmt: skip
    """A BacktestResult in its encoded shape, for metrics edge cases the engine cannot easily
    reach (a flat curve, a book that goes to zero, a benchmark with gaps)."""
    cfg = config or config_json(start=dates[0].isoformat(), end=dates[-1].isoformat())
    inv = invested if invested is not None else [str(D(e) * D("0.9")) for e in equity]
    return _enc(
        {
            "config": cfg,
            "dates": dates,
            "equity": equity,
            "cash": [str(D(e) - D(i)) for e, i in zip(equity, inv, strict=True)],
            "invested": inv,
            "benchmark": benchmark if benchmark is not None else [None] * len(dates),
            "trades": trades or [],
            "holdings": [],
            "delistings": [
                {"date": dates[-1], "instrument_id": 108, "symbol": "HHH", "quantity": 1,
                 "price": "1.00", "priced_on": dates[-1], "proceeds": "1.00"}
            ] * delistings,  # fmt: skip
            "total_costs": total_costs,
            "dividends_credited": "0",
            "rebalance_dates": [dates[0]],
            "blind_rebalances": [],
            "adjustment_coverage": None,
            "unexecuted_fills": [],
            "notes": [],
            "data_version": None,
        }
    )


Case = tuple[str, dict[str, Any], str]


def cases() -> Iterator[Case]:  # noqa: PLR0915 - one block per function, in order
    from baskfy_core.backtest import (
        AdjustmentCoverage,
        BacktestConfig,
        BacktestData,
        PricePanel,
        run_backtest,
    )

    cal_iso = [d.isoformat() for d in CAL]

    # ---- rebalance_dates ------------------------------------------------------------------
    f = "baskfy_core.backtest.rebalance_dates"
    long_cal = calendar(dt.date(2020, 12, 21), 80, holidays=())  # crosses ISO week 53 of 2020
    for cal_, start, end, spec, note in (
        (CAL, "2024-01-01", "2024-06-28", {"frequency": "monthly", "day": "last_trading_day"},
         "monthly, last trading day; first day always included"),
        (CAL, "2024-01-01", "2024-06-28", {"frequency": "monthly", "day": "first_trading_day"},
         "monthly, first trading day"),
        (CAL, "2024-01-03", "2024-03-29", {"frequency": "weekly", "day": "last_trading_day"},
         "weekly; start mid-week is still a rebalance; holiday Fri 2024-01-26 moves the week end"),
        (CAL, "2024-01-01", "2024-07-17", {"frequency": "quarterly", "day": "last_trading_day"},
         "quarterly; the final partial quarter takes its last available day"),
        (CAL, "2024-01-01", "2024-03-29", {"frequency": "fortnightly", "day": "first_trading_day"},
         "fortnightly anchored on ISO week // 2"),
        ([d for d in long_cal], "2020-12-21", "2021-04-09",
         {"frequency": "fortnightly", "day": "last_trading_day"},
         "fortnightly across ISO week 53 (2020) into 2021"),
        ([d for d in long_cal], "2020-12-21", "2021-02-26",
         {"frequency": "weekly", "day": "first_trading_day"}, "weekly across the ISO year boundary"),
        (CAL, "2024-01-27", "2024-01-28", {"frequency": "monthly", "day": "last_trading_day"},
         "window holds no trading day (a weekend): empty"),
        (CAL, "2023-06-01", "2024-01-05", {"frequency": "monthly", "day": "last_trading_day"},
         "start before the calendar: window clipped to the calendar"),
        (CAL, "2024-03-22", "2024-03-26", {"frequency": "monthly", "day": "last_trading_day"},
         "boundary: start and end are inclusive; 2024-03-25 is a holiday"),
    ):  # fmt: skip
        yield f, {"calendar": [d.isoformat() for d in cal_], "start": start, "end": end,
                  "spec": spec}, note  # fmt: skip

    # ---- shift_schedule -------------------------------------------------------------------
    f = "baskfy_core.backtest.shift_schedule"
    month_ends = [d.isoformat() for d in _rebal(CAL[0], CAL[-1])]
    for dates, offset, note in (
        (month_ends, 1, "+1 trading day; the final calendar day falls off the end"),
        (month_ends, -1, "-1 trading day; the first calendar day falls off the front"),
        (month_ends, 0, "zero offset is the identity"),
        (["2024-01-25", "2024-01-29"], 1, "+1 across the 2024-01-26 holiday"),
        (["2024-02-01", "2024-02-02"], 1, "two dates shifting onto distinct days stay distinct"),
        (["2024-01-27", "2024-02-05", "2024-02-05"], -2,
         "a date not in the calendar is dropped; duplicates collapse"),
        ([], 1, "empty schedule"),
    ):  # fmt: skip
        yield f, {"calendar": cal_iso, "dates": dates, "offset": offset}, note

    # ---- apply_position_limits ------------------------------------------------------------
    f = "baskfy_core.backtest.apply_position_limits"
    lim = lambda mx, mn: {"max_weight": mx, "min_weight": mn}  # noqa: E731
    for weights, limits, note in (
        ({}, lim("0.10", "0.01"), "empty: empty"),
        ({"1": "1", "2": "1", "3": "1"}, lim("0.5", "0.1"), "equal thirds: remainder placement"),
        ({"1": "1", "2": "1", "3": "1"}, lim("0.10", "0.01"),
         "3 * 0.10 < 1 is infeasible: ceiling relaxed to 1/n"),
        ({str(i): "1" for i in range(1, 121)}, lim("0.10", "0.01"),
         "120 names * 0.01 > 1 is infeasible: floor relaxed to 1/n"),
        ({"1": "10", "2": "3", "3": "2", "4": "1", "5": "1"}, lim("0.30", "0.05"),
         "water-fill: the cap binds and its excess is redistributed"),
        ({"1": "50", "2": "20", "3": "10", "4": "1", "5": "0.5", "6": "0.2"}, lim("0.35", "0.08"),
         "ceilings resolve before floors"),
        ({"1": "0", "2": "0", "3": "0", "4": "0"}, lim("0.30", "0.01"),
         "all-zero raw weights: shared equally"),
        ({"7": "1", "3": "2", "9": "3"}, lim("1", "0"), "no binding limits: proportional"),
        ({"1": "1", "2": "1", "3": "1", "4": "1", "5": "1", "6": "1", "7": "1"}, lim("0.2", "0"),
         "sevenths: remainder goes to the largest name with room, lowest id on ties"),
        ({"1": "0.4", "2": "0.4", "3": "0.2"}, lim("0.4", "0.2"),
         "boundary: weights exactly at both bounds"),
    ):  # fmt: skip
        yield f, {"weights": weights, "limits": limits}, note

    # ---- raw_weights ----------------------------------------------------------------------
    f = "baskfy_core.backtest.raw_weights"

    def sel(inst: int, rank: int, cap: str | None = "1000", vol: str | None = "0.2") -> dict[str, Any]:
        return {"instrument_id": inst, "symbol": SYMBOLS[inst], "name": NAMES[inst], "rank": rank,
                "marketcap_cr": cap, "volatility": vol}  # fmt: skip

    rows4 = [sel(101, 3, "1200.5", "0.18"), sel(102, 1, "450", "0.32"),
             sel(103, 7, "30000", "0.22"), sel(104, 2, "800", "0.41")]  # fmt: skip
    for scheme, rows, note in (
        ("equal", rows4, "equal: 1 each"),
        ("rank", rows4, "rank: n..1 within the selected set, absolute ranks ignored"),
        ("rank", [sel(101, 2), sel(102, 2), sel(103, 1)], "rank ties broken by instrument_id"),
        ("marketcap", rows4, "marketcap: the cap itself"),
        ("inverse_volatility", rows4, "inverse_volatility: 1/vol"),
        ("marketcap", [sel(101, 1), sel(102, 2, cap=None)], "null marketcap refused"),
        ("inverse_volatility", [sel(101, 1), sel(102, 2, vol="0")], "zero volatility refused"),
        ("equal", [], "empty selection"),
    ):  # fmt: skip
        yield f, {"scheme": scheme, "rows": rows}, note

    # ---- CostSpec.charge ------------------------------------------------------------------
    f = "baskfy_core.backtest.CostSpec.charge"
    notionals = ["0", "100000.00", "-100000.00", "12345.67", "0.33", "1", "99999999.99"]
    default_costs = {"brokerage_bps": "3", "stt_bps": "10", "slippage_bps": "15",
                     "impact_model": "fixed"}  # fmt: skip
    for costs, scale, note in (
        (default_costs, None, "docs/10 defaults (28 bps); sign ignored; half-up to the paisa"),
        (default_costs, "0.75", "fragility -25%"),
        (default_costs, "1.25", "fragility +25%"),
        ({"brokerage_bps": "0", "stt_bps": "0", "slippage_bps": "0", "impact_model": "fixed"},
         None, "zero cost"),
        ({"brokerage_bps": "2.5", "stt_bps": "0.1", "slippage_bps": "0.05",
          "impact_model": "fixed"}, None, "fractional bps: rounding at the half paisa"),
    ):  # fmt: skip
        yield f, {"costs": costs, "scale": scale, "notionals": notionals}, note

    # ---- AdjustmentCoverage.caveats -------------------------------------------------------
    f = "baskfy_core.backtest.AdjustmentCoverage.caveats"

    def cov(af: str | None, at: str | None, saf: str | None, splice: str | None,
            today: bool) -> dict[str, Any]:  # fmt: skip
        return {"actions_from": af, "actions_to": at, "self_adjusted_from": saf,
                "splice_on": splice, "as_of_today": today}  # fmt: skip

    for coverage, start, end, note in (
        (cov(None, None, None, None, True), "2017-01-02", "2026-09-10",
         "no actions at all and no segment: unadjusted-in-fact + as-of-today"),
        (cov("2024-01-02", "2026-09-01", "2024-01-01", "2024-01-01", True), "2017-01-02",
         "2026-09-10", "the deployed box: one-day gap (singular), splice, as-of-today"),
        (cov("2024-01-10", "2026-09-01", "2024-01-01", "2024-01-01", False), "2024-01-01",
         "2026-09-10", "plural gap; start on the splice day is not before it; not as-of-today"),
        (cov("2023-12-01", "2026-09-01", "2024-01-01", "2024-01-01", False), "2017-01-02",
         "2023-06-30", "run ends before the self-adjusted segment: only the splice caveat"),
        (cov("2015-01-01", "2026-09-01", None, None, False), "2017-01-02", "2026-09-10",
         "single segment, actions cover the window: nothing to say"),
        (cov("2020-01-01", "2026-09-01", None, None, True), "2017-01-02", "2026-09-10",
         "single segment starting before the first action: 1095-day gap"),
        (cov(None, None, "2024-01-01", "2024-01-01", False), "2024-06-01", "2026-09-10",
         "segment reached, no actions on record"),
    ):  # fmt: skip
        yield f, {"coverage": coverage, "start": start, "end": end}, note

    # ---- year_fraction / month_key / month_end -------------------------------------------
    for start, end, note in (
        ("2024-01-01", "2025-01-01", "a leap year: 366/365.25"),
        ("2023-01-01", "2024-01-01", "a common year"),
        ("2024-01-01", "2024-01-01", "zero length"),
        ("2024-03-01", "2024-02-01", "negative span"),
        ("2011-01-01", "2026-10-29", "the D5 backfill span"),
    ):
        yield "baskfy_core.backtest.year_fraction", {"start": start, "end": end}, note
    for day, note in (
        ("2024-01-31", "January"), ("2024-02-10", "leap February"),
        ("2023-02-10", "common February"), ("2100-02-01", "century non-leap February"),
        ("2024-12-01", "December"), ("0999-04-15", "zero-padded year"),
    ):  # fmt: skip
        yield "baskfy_core.backtest.month_key", {"day": day}, note
        yield "baskfy_core.backtest.month_end", {"day": day}, note

    # ---- PricePanel -----------------------------------------------------------------------
    f = "baskfy_core.backtest.PricePanel"
    short = CAL[:15]
    short_walks = {k: (v[0][:15], v[1][:15]) for k, v in WALKS.items()}
    gappy = price_frame(
        short, short_walks,
        drop={(101, short[3]), (101, short[4]), (102, short[0])},
        null_open={(103, short[2])}, stop_after={104: short[5], 105: short[9]},
        raw_split=(106, short[7], 2.0),
    )  # fmt: skip
    ops = [
        {"op": "index_of", "day": short[2].isoformat()},
        {"op": "index_of", "day": "2024-01-06"},
        {"op": "close_on", "instrument_id": 101, "position": 3},
        {"op": "close_on", "instrument_id": 101, "position": 5},
        {"op": "close_on", "instrument_id": 999, "position": 5},
        {"op": "open_on", "instrument_id": 103, "position": 2},
        {"op": "open_on", "instrument_id": 106, "position": 6},
        {"op": "close_on", "instrument_id": 106, "position": 6},
        {"op": "last_close_at_or_before", "instrument_id": 101, "position": 4},
        {"op": "last_close_at_or_before", "instrument_id": 102, "position": 0},
        {"op": "last_close_at_or_before", "instrument_id": 104, "position": 14},
        {"op": "last_close_at_or_before", "instrument_id": 999, "position": 3},
        {"op": "liquidation_day", "instrument_id": 104, "delisted_on": None},
        {"op": "liquidation_day", "instrument_id": 105, "delisted_on": None},
        {"op": "liquidation_day", "instrument_id": 104, "delisted_on": short[3].isoformat()},
        {"op": "liquidation_day", "instrument_id": 107, "delisted_on": "2024-01-06"},
        {"op": "liquidation_day", "instrument_id": 107, "delisted_on": "2030-01-01"},
        {"op": "liquidation_day", "instrument_id": 107, "delisted_on": None},
        {"op": "liquidation_day", "instrument_id": 999, "delisted_on": None},
    ]
    yield f, _enc({"calendar": short, "prices": gappy, "price_only": False, "ops": ops}), (
        "adjusted panel: missing bars are null (never zero, never forward-filled); a null open "
        "keeps its close; 104 stops after index 5 (liquidation index 5+5+1=11), 105 after "
        "index 9 (9 < 15-5, so 15: one past the calendar); delisted_on on a weekend maps to the "
        "next trading day, after the calendar to no trigger; the earlier trigger wins; an "
        "unknown id is None"
    )
    yield f, _enc({"calendar": short, "prices": gappy, "price_only": True, "ops": ops}), (
        "same frame read as price_only: 106's raw pre-split prints are 2x the adjusted series "
        "before the ex-date (adjusted vs raw)"
    )
    off_cal = price_frame(short, short_walks).with_columns(
        pl.when(pl.col("date") == short[1]).then(pl.lit(dt.date(2024, 1, 6))).otherwise(
            pl.col("date")).alias("date"))  # fmt: skip
    yield f, _enc({"calendar": short, "prices": off_cal, "price_only": False, "ops": []}), (
        "bars on a non-calendar date are refused"
    )
    yield f, _enc({"calendar": short, "prices": price_frame(short, short_walks).drop("open"),
                   "price_only": False, "ops": []}), "missing 'open' column is refused"  # fmt: skip
    empty = price_frame(short, short_walks).head(0)
    yield f, _enc({"calendar": short, "prices": empty, "price_only": False,
                   "ops": [{"op": "close_on", "instrument_id": 101, "position": 0},
                           {"op": "liquidation_day", "instrument_id": 101,
                            "delisted_on": short[4].isoformat()}]}), (
        "an empty panel: every read is None, delisted_on still schedules"
    )  # fmt: skip

    # ---- PointInTimeReader (the look-ahead guard) -----------------------------------------
    f = "baskfy_core.backtest.PointInTimeReader"
    d5, d6, d7 = CAL[20], CAL[21], CAL[22]
    divs = pl.DataFrame(
        [{"date": d6, "instrument_id": 101, "amount": 2.5}],
        schema={"date": pl.Date, "instrument_id": pl.Int64, "amount": pl.Float64},
    )
    pit_data = data_json(
        cal=short + CAL[15:25],
        prices=price_frame(CAL[:25], {k: (v[0][:25], v[1][:25]) for k, v in WALKS.items()}),
        screens={
            d5: screen_frame(d5),
            d6: screen_frame(d6, stamp=d7),  # shifted forward one day: the injected bug
            d7: screen_frame(d7).drop("date"),
        },
        benchmark=benchmark_frame(CAL[:25]),
        dividends=divs,
    )
    yield f, {"data": pit_data, "ops": [
        {"op": "as_of"},
        {"op": "advance_to", "day": d5.isoformat()},
        {"op": "as_of"},
        {"op": "screen_on", "day": d5.isoformat()},
        {"op": "price_close", "instrument_id": 101, "day": d5.isoformat()},
        {"op": "price_open", "instrument_id": 101, "day": d6.isoformat()},
        {"op": "last_close", "instrument_id": 102, "day": d5.isoformat()},
        {"op": "benchmark_to", "day": CAL[3].isoformat()},
        {"op": "dividends_on", "day": d5.isoformat()},
        {"op": "advance_to", "day": d6.isoformat()},
        {"op": "screen_on", "day": d6.isoformat()},
        {"op": "dividends_on", "day": d6.isoformat()},
        {"op": "advance_to", "day": d5.isoformat()},
        {"op": "advance_to", "day": d7.isoformat()},
        {"op": "screen_on", "day": d7.isoformat()},
        {"op": "screen_on", "day": CAL[10].isoformat()},
        {"op": "price_close", "instrument_id": 999, "day": d7.isoformat()},
    ]}, ("the guard: unset cursor refused; reads at/before the cursor pass; a screen stamped a "
         "day after its key is LookAheadError; a future price is LookAheadError; the cursor never "
         "moves back; a frame without 'date' is refused; a missing screen is refused")  # fmt: skip
    yield f, {"data": data_json(cal=CAL[:25], prices=price_frame(
        CAL[:25], {k: (v[0][:25], v[1][:25]) for k, v in WALKS.items()}), screens={}),
              "ops": [{"op": "advance_to", "day": d5.isoformat()},
                      {"op": "benchmark_to", "day": d5.isoformat()},
                      {"op": "dividends_on", "day": d5.isoformat()},
                      {"op": "price_close", "instrument_id": 101, "day": "2024-01-06"}]}, (
        "no benchmark, no dividend schedule, a non-calendar day: each refused"
    )  # fmt: skip

    # ---- run_backtest ---------------------------------------------------------------------
    f = "baskfy_core.backtest.run_backtest"
    s, e = dt.date(2024, 1, 1), dt.date(2024, 4, 30)
    monthly = _rebal(s, e)
    base_cfg = dict(start=s.isoformat(), end=e.isoformat(),
                    selection={"top_n": 3, "hold_buffer": 1})  # fmt: skip

    def run_case(cfg: dict[str, Any], data: dict[str, Any], note: str,
                 schedule: list[dt.date] | None = None) -> Case:  # fmt: skip
        inputs: dict[str, Any] = {"config": config_json(**cfg), "data": data}
        if schedule is not None:
            inputs["schedule"] = [d.isoformat() for d in schedule]
        return f, inputs, note

    yield run_case(base_cfg, data_json(screens=_screens_for(monthly)),
                   "baseline: monthly last-trading-day, equal weight, top 3 + buffer 1, default "
                   "costs; fills at the next open")  # fmt: skip
    weekly = _rebal(s, dt.date(2024, 3, 1), frequency="weekly", day="first_trading_day")
    yield run_case(
        {"start": s.isoformat(), "end": "2024-03-01",
         "rebalance": {"frequency": "weekly", "day": "first_trading_day"},
         "selection": {"top_n": 4, "hold_buffer": 0}, "weighting": "rank",
         "position_limits": {"max_weight": "0.45", "min_weight": "0.05"}},
        data_json(screens=_screens_for(weekly, lookback=5)),
        "weekly, rank weighting, strict top-4 (buffer 0); exits and re-entries",
    )  # fmt: skip
    yield run_case(
        {**base_cfg, "selection": {"top_n": 5, "hold_buffer": 2}, "weighting": "inverse_volatility",
         "position_limits": {"max_weight": "0.30", "min_weight": "0.12"}},
        data_json(screens=_screens_for(monthly)),
        "inverse-volatility weights clipped to [0.12, 0.30] and renormalised",
    )  # fmt: skip
    yield run_case(
        {**base_cfg, "weighting": "marketcap",
         "position_limits": {"max_weight": "0.40", "min_weight": "0.05"}},
        data_json(screens=_screens_for(monthly, tie=(101, 102))),
        "marketcap weights with the 40% cap binding; two names tie on rank",
    )  # fmt: skip
    yield run_case(
        {**base_cfg, "weighting": "marketcap"},
        data_json(screens=_screens_for(monthly, null_marketcap=frozenset(IDS))),
        "marketcap weighting with null marketcap_cr: BacktestDataError",
    )  # fmt: skip
    # Delisting: two names the baseline holds through February — one delisted by reference data
    # (on a Saturday, so effective the next trading day), one whose series goes quiet. Later
    # screens no longer list either (a dead name is not ranked).
    baseline = run_backtest(
        BacktestConfig.model_validate(base_cfg), decode_data(data_json(screens=_screens_for(monthly)))
    )
    held_feb = [h.instrument_id for h in baseline.holdings if h.rebalance_date == monthly[1]]
    gone, quiet = held_feb[0], held_feb[1]
    quiet_after = dt.date(2024, 2, 5)
    dead_screens = {
        day: screen_frame(day, exclude=frozenset({gone, quiet}) if day > quiet_after else frozenset())
        for day in monthly
    }
    yield run_case(
        base_cfg,
        data_json(
            prices=price_frame(stop_after={quiet: quiet_after}),
            screens=dead_screens,
            delistings={gone: dt.date(2024, 2, 10)},
        ),
        f"delistings: {gone} delisted 2024-02-10 (Sat) is liquidated at its last close on "
        f"2024-02-12; {quiet} prints no bar after {quiet_after} and is liquidated six trading "
        "days after its last bar (tolerance 5), at that last close, never forward-filled",
    )  # fmt: skip
    fill_day = CAL[CAL.index(monthly[1]) + 1]
    yield run_case(
        base_cfg,
        data_json(prices=price_frame(null_open={(i, fill_day) for i in IDS}),
                  screens=_screens_for(monthly)),  # fmt: skip
        f"thin session: no name has an opening print on fill day {fill_day}: unexecuted_fills, "
        "book unchanged, held names marked at their last close",
    )  # fmt: skip
    partial_day = CAL[CAL.index(monthly[2]) + 1]
    yield run_case(
        base_cfg,
        data_json(prices=price_frame(null_open={(i, partial_day) for i in IDS[:5]}),
                  screens=_screens_for(monthly)),  # fmt: skip
        f"partial thin session on {partial_day}: five names unfillable (note names them), "
        "the rest fill",
    )  # fmt: skip
    blind_screens = _screens_for(monthly)
    blind_screens[monthly[2]] = screen_frame(monthly[2], exclude=frozenset(IDS))
    yield run_case(
        base_cfg, data_json(screens=blind_screens),
        f"the screen returns nothing on {monthly[2]}: blind rebalance, the book goes to cash",
    )  # fmt: skip
    peek = _screens_for(monthly)
    peek[monthly[1]] = screen_frame(monthly[1], stamp=CAL[CAL.index(monthly[1]) + 1])
    yield run_case(base_cfg, data_json(screens=peek),
                   "look-ahead injected (a screen row dated the day after its rebalance): "
                   "LookAheadError, house rule 5")  # fmt: skip
    div_rows = [
        {"date": CAL[45], "instrument_id": i, "amount": 1.75} for i in IDS
    ] + [{"date": CAL[70], "instrument_id": i, "amount": None} for i in IDS[:2]] + [
        {"date": CAL[70], "instrument_id": i, "amount": 3.0} for i in IDS[2:]
    ]
    divs = pl.DataFrame(div_rows, schema={"date": pl.Date, "instrument_id": pl.Int64,
                                          "amount": pl.Float64})  # fmt: skip
    yield run_case({**base_cfg, "dividends": "cash"},
                   data_json(screens=_screens_for(monthly), dividends=divs),
                   "cash dividends credited on ex-date to held names only; null amounts skipped")
    yield run_case({**base_cfg, "dividends": "reinvest"}, data_json(screens=_screens_for(monthly)),
                   "reinvest without a dividend schedule: BacktestConfigError before any day")
    bench_nulls = frozenset({CAL[12], CAL[13], CAL[40]})
    yield run_case(
        {**base_cfg, "cash_policy": "benchmark", "selection": {"top_n": 2, "hold_buffer": 0},
         "position_limits": {"max_weight": "0.30", "min_weight": "0.01"}},
        data_json(screens=blind_screens, benchmark=benchmark_frame(CAL, nulls=bench_nulls)),
        f"cash_policy benchmark: uninvested cash (all of it after the blind {monthly[2]}) earns "
        "the benchmark's daily return; a null benchmark day is a flat day",
    )  # fmt: skip
    yield run_case(
        {**base_cfg, "risk_overlay": {"enabled": True, "rule": "index_above_200dma"}},
        data_json(screens=_screens_for(monthly), benchmark=benchmark_frame(CAL)),
        "risk overlay on with fewer than 200 benchmark observations: noted, stays invested",
    )  # fmt: skip
    # Long benchmark history before the run so the 200-dma exists; crash at the end.
    ov_s, ov_e = CAL[100], CAL[-1]
    ov_dates = _rebal(ov_s, ov_e, frequency="weekly", day="last_trading_day")
    pre = calendar(dt.date(2023, 2, 1), 230, holidays=())
    hist = [d for d in pre if d < CAL[0]] + CAL
    yield run_case(
        {"start": ov_s.isoformat(), "end": ov_e.isoformat(),
         "rebalance": {"frequency": "weekly", "day": "last_trading_day"},
         "selection": {"top_n": 3, "hold_buffer": 1},
         "risk_overlay": {"enabled": True, "rule": "index_above_200dma"}},
        data_json(screens=_screens_for(ov_dates),
                  benchmark=benchmark_frame(hist, crash_from=len(hist) - 30)),
        "risk overlay with 200+ observations: invested while above the 200-dma, to cash after "
        "the benchmark breaks below it",
    )  # fmt: skip
    split_id, split_day = 103, CAL[40]
    split_prices = price_frame(raw_split=(split_id, split_day, 2.0))
    yield run_case(base_cfg, data_json(prices=split_prices, screens=_screens_for(monthly)),
                   f"adjusted panel across a 1:2 split of {split_id} on {split_day}: continuous")
    yield run_case(
        base_cfg,
        data_json(prices=split_prices, price_only=True, screens=_screens_for(monthly)),
        f"the same panel read as price_only (raw prints): {split_id} halves on {split_day}, "
        "the book shows a fake loss — why the adjusted series is the default",
    )  # fmt: skip
    custom = [CAL[0], CAL[17], CAL[33], e if e in CAL else CAL[CAL.index(monthly[-1])]]
    yield run_case(base_cfg, data_json(screens=_screens_for(custom)),
                   "schedule override; a decision on the window's last day is never filled",
                   schedule=custom)  # fmt: skip
    yield run_case(
        {**base_cfg, "initial_capital": "2500", "selection": {"top_n": 4, "hold_buffer": 0}},
        data_json(screens=_screens_for(monthly)),
        "tiny account: whole shares round down and buys are trimmed to cash; target vs actual "
        "weight diverge",
    )  # fmt: skip
    yield run_case({**base_cfg, "start": "2025-01-01", "end": "2025-02-01"},
                   data_json(screens=_screens_for(monthly)),
                   "no trading day in the window: BacktestDataError")  # fmt: skip
    yield run_case(base_cfg, data_json(screens=_screens_for(monthly)),
                   "a schedule date that is not a trading day: BacktestDataError",
                   schedule=[CAL[0], dt.date(2024, 1, 26)])  # fmt: skip
    yield run_case(base_cfg, data_json(screens=_screens_for(monthly[:2])),
                   "a rebalance date with no precomputed screen: BacktestDataError")

    # ---- run_fragility --------------------------------------------------------------------
    f = "baskfy_core.backtest.run_fragility"
    fr_s, fr_e = dt.date(2024, 1, 1), dt.date(2024, 3, 29)
    fr_days = [d for d in CAL if fr_s <= d <= fr_e]
    fr_cfg = config_json(start=fr_s.isoformat(), end=fr_e.isoformat(),
                         selection={"top_n": 3, "hold_buffer": 1})  # fmt: skip
    yield f, {"config": fr_cfg, "data": data_json(screens=_screens_for(fr_days))}, (
        "base + costs -25%/+25% + rebalance -1/+1 trading day; every day has a screen"
    )
    yield f, {
        "config": config_json(start=fr_s.isoformat(), end=fr_e.isoformat(), weighting="rank",
                              selection={"top_n": 2, "hold_buffer": 0},
                              rebalance={"frequency": "weekly", "day": "last_trading_day"}),
        "data": data_json(screens=_screens_for(fr_days, lookback=5)),
    }, "weekly rank-weighted fragility"  # fmt: skip
    yield f, {"config": fr_cfg, "data": data_json(screens=_screens_for(_rebal(fr_s, fr_e)))}, (
        "shifted dates have no screen: the offset probe raises rather than reusing a neighbour"
    )

    # ---- metrics: artefacts ---------------------------------------------------------------
    f = "baskfy_core.backtest_metrics.drawdown_series"
    d8 = [d.isoformat() for d in CAL[:8]]
    for equity, note in (
        ([], "empty"),
        (["100", "110", "121", "130", "131", "140", "150", "160"], "monotonic: no drawdown"),
        (["100", "120", "90", "60", "80", "120", "125", "100"], "drawdown, recovery, new peak"),
        (["0", "0", "50", "40", "40", "60", "30", "30"], "zero peak at the start reads 0.0"),
        (["100", "100", "100", "100", "100", "100", "100", "100"], "flat"),
    ):  # fmt: skip
        yield f, {"dates": d8[: len(equity)], "equity": equity}, note

    f = "baskfy_core.backtest_metrics.monthly_returns"
    for dates, equity, note in (
        ([], [], "empty"),
        (d8, ["100", "101", "102", "99", "98", "100", "103", "104"], "one month (Jan): from the "
         "opening value"),
        ([d.isoformat() for d in CAL[18:26]],
         ["100", "105", "110", "108", "112", "115", "90", "95"], "January into February"),
        (["2024-11-29", "2024-12-31", "2025-01-02", "2025-01-31"], ["100", "110", "105", "121"],
         "across a year end; the first month measures from its own first value"),
        (["2024-01-31", "2024-02-29", "2024-03-29"], ["0", "100", "120"],
         "a non-positive previous month reads 0.0"),
    ):  # fmt: skip
        yield f, {"dates": dates, "equity": equity}, note

    f = "baskfy_core.backtest_metrics.downsample_curve"
    eq40 = _equity_path(40, 11)
    d40 = [d.isoformat() for d in CAL[:40]]
    bm40: list[str | None] = [str(D(20000) + i) for i in range(40)]
    bm40[5] = None
    for dates, equity, bench, limit, note in (
        ([], [], [], None, "empty"),
        (d40, eq40, bm40, None, "below the default limit: every point"),
        (d40, eq40, bm40, 7, "stride 6; the last point is kept"),
        (d40, eq40, [], 10, "stride 4 lands on the last point; no benchmark"),
        (d40, eq40, bm40, 1, "limit 1: first and last"),
    ):  # fmt: skip
        yield f, {"dates": dates, "equity": equity, "benchmark": bench, "limit": limit}, note

    f = "baskfy_core.backtest_metrics.round_trips"
    for trades, note in (
        ([], "empty"),
        ([_trade("2024-01-02", 101, "buy", 10, "100.00", "2.80", "enter"),
          _trade("2024-02-01", 101, "sell", 10, "120.00", "3.36", "exit", "193.84")],
         "one winning round trip"),
        ([_trade("2024-01-02", 102, "buy", 10, "100.00", "2.80", "enter"),
          _trade("2024-01-15", 102, "sell", 4, "90.00", "1.01", "rebalance", "-42.13"),
          _trade("2024-02-01", 102, "sell", 6, "95.00", "1.60", "exit", "-33.28")],
         "partial trims then exit: one losing round trip"),
        ([_trade("2024-01-02", 103, "buy", 5, "50.00", "0.70", "enter"),
          _trade("2024-01-20", 103, "sell", 5, "10.00", "0.14", "delist", "-200.84")],
         "closed by a delisting"),
        ([_trade("2024-01-02", 101, "buy", 3, "10.00", "0.08", "enter"),
          _trade("2024-01-03", 104, "buy", 2, "20.00", "0.11", "enter"),
          _trade("2024-01-04", 101, "sell", 3, "10.00", "0.08", "exit", "-0.16"),
          _trade("2024-01-05", 101, "buy", 1, "11.00", "0.03", "enter"),
          _trade("2024-01-06", 104, "sell", 2, "21.00", "0.12", "exit", "1.77"),
          _trade("2024-01-07", 101, "sell", 1, "12.00", "0.03", "exit", "0.94")],
         "interleaved names; a re-entry is a new round trip; a zero-or-less P&L is not a win"),
        ([_trade("2024-01-02", 105, "buy", 7, "33.00", "0.65", "enter"),
          _trade("2024-01-09", 105, "buy", 3, "35.00", "0.29", "rebalance")],
         "still open at the end: no round trip"),
        ([_trade("2024-01-02", 106, "buy", 4, "25.00", "0.28", "enter"),
          _trade("2024-01-09", 106, "sell", 4, "25.07", "0.28", "exit", "0.00")],
         "break-even (pnl 0) counts as a loss"),
    ):  # fmt: skip
        yield f, {"trades": trades}, note

    f = "baskfy_core.backtest_metrics.rolling_distribution"
    long_days = calendar(dt.date(2023, 1, 2), 420, holidays=())
    leap_days = [dt.date(2024, 2, 28), dt.date(2024, 2, 29), dt.date(2025, 2, 27),
                 dt.date(2025, 2, 28), dt.date(2025, 3, 3)]  # fmt: skip
    for dates, equity, note in (
        ([], [], "empty"),
        (["2024-01-02"], ["100"], "one observation"),
        ([d.isoformat() for d in CAL[:40]], eq40, "shorter than a year: no window"),
        ([d.isoformat() for d in long_days], _equity_path(420, 5), "~1.6 years of weekdays"),
        ([d.isoformat() for d in long_days], _equity_path(420, 9, drift=-0.0012),
         "a losing path: negative_share high"),
        ([d.isoformat() for d in leap_days], ["100", "100", "110", "90", "95"],
         "29 Feb maps to 28 Feb next year"),
        ([d.isoformat() for d in long_days[:300]], ["0"] * 10 + _equity_path(290, 3),
         "non-positive opening values are skipped"),
    ):  # fmt: skip
        yield f, {"dates": dates, "equity": equity}, note

    # ---- metrics: compute_metrics / metrics_hash ------------------------------------------
    def engine_result(cfg: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
        result = run_backtest(BacktestConfig.model_validate(cfg), decode_data(data))
        return _enc(result)

    f = "baskfy_core.backtest_metrics.compute_metrics"
    base_result = engine_result(config_json(**base_cfg),
                                data_json(screens=_screens_for(monthly),
                                          benchmark=benchmark_frame(CAL)))  # fmt: skip
    nobench_result = engine_result(config_json(**base_cfg), data_json(screens=_screens_for(monthly)))
    rf_cfg = config_json(**base_cfg, risk_free_rate="0.065",
                         risk_free_curve=[["2023-12-01", "0.068"], ["2024-02-01", "0.071"],
                                          ["2024-04-01", "0.066"]])  # fmt: skip
    rf_result = engine_result(rf_cfg, data_json(screens=_screens_for(monthly),
                                                benchmark=benchmark_frame(CAL, nulls=bench_nulls)))
    yr_s, yr_e = long_days[0], long_days[-1]
    flat_rf = config_json(start=yr_s.isoformat(), end=yr_e.isoformat(), risk_free_rate="0.05")
    long_eq = _equity_path(420, 21)
    long_bm: list[str | None] = [str(D(x) / D(50)) for x in _equity_path(420, 22, drift=0.0003)]
    for i in range(0, 60):
        long_bm[i] = None  # leading gap: benchmark CAGR over its covered span only
    gappy_bm: list[str | None] = list(long_bm)
    for i in range(100, 420, 13):
        gappy_bm[i] = None
    for result, note in (
        (base_result, "the baseline engine run with a benchmark"),
        (nobench_result, "no benchmark: alpha, beta, tracking error, IR are None"),
        (rf_result, "risk-free curve forward-filled (fallback before its first point); benchmark "
         "with null days (tracking error over observed days only)"),
        (_synthetic_result([d for d in long_days], long_eq, benchmark=long_bm, config=flat_rf,
                           total_costs="1234.56", delistings=1),
         "1.6 years, flat 5% rf, benchmark missing its first 60 days, rolling 12m populated"),
        (_synthetic_result([d for d in long_days], long_eq, benchmark=gappy_bm, config=flat_rf),
         "the same with every 13th benchmark level missing"),
        (_synthetic_result(CAL[:30], ["1000000.00"] * 30),
         "flat curve: zero volatility; Sharpe/Sortino None; no drawdown"),
        (_synthetic_result(CAL[:6], ["1000000.00", "800000.00", "300000.00", "50000.00",
                                     "0.00", "0.00"], invested=["0"] * 6),
         "a book that goes to zero: CAGR -1.0, max drawdown -1.0, exposure 0"),
        (_synthetic_result(CAL[:1], ["1000000.00"],
                           config=config_json(start=CAL[0].isoformat(), end=CAL[1].isoformat())),
         "a single day: zero-length window, no returns"),
        (_synthetic_result(CAL[:25], _equity_path(25, 4, drift=0.002),
                           trades=[_trade("2024-01-02", 101, "buy", 100, "500.00", "140.00", "enter"),
                                   _trade("2024-01-20", 101, "sell", 100, "560.00", "156.80",
                                          "exit", "5703.20"),
                                   _trade("2024-01-03", 102, "buy", 50, "200.00", "28.00", "enter"),
                                   _trade("2024-01-25", 102, "sell", 50, "180.00", "25.20",
                                          "exit", "-1053.20")],
                           total_costs="350.00"),
         "hit rate, average win/loss and turnover from a hand-written fill log"),
    ):  # fmt: skip
        yield f, {"result": result}, note

    f = "baskfy_core.backtest_metrics.metrics_hash"
    for result, version, note in (
        (base_result, 42, "the baseline run at data_version 42"),
        (base_result, 43, "same run, data_version 43: a different hash"),
        (base_result, None, "same run, no data_version"),
        (nobench_result, 42, "no benchmark"),
        (rf_result, 42, "with a risk-free curve in the config"),
    ):
        yield f, {"result": result, "data_version": version}, note

    # keep imports referenced for type checkers
    _ = (AdjustmentCoverage, BacktestData, PricePanel)


# =============================================================================================
# Dump
# =============================================================================================


def render_case(fn: str, inputs: dict[str, Any], note: str) -> str:
    golden = _golden()
    inputs = _enc(inputs)  # what is stored is exactly what is decoded
    doc = golden.document(
        fn=fn, inputs=inputs, output=CALLS[fn](inputs), notes=f"{note}. {NOTES}", stable=True
    )
    return golden.render(doc)


def dump_backtest() -> list[Path]:
    golden = _golden()
    directory = golden.GOLDEN_DIR / LANE
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    counters: dict[str, int] = {}
    for fn, inputs, note in cases():
        stem = fn.removeprefix("baskfy_core.").replace("backtest_metrics.", "").replace(
            "backtest.", "")  # fmt: skip
        counters[stem] = counters.get(stem, 0) + 1
        path = directory / f"{stem}.case_{counters[stem]:03d}.json"
        path.write_text(render_case(fn, inputs, note), encoding="utf-8")
        written.append(path)
    return written


if __name__ == "__main__":
    paths = dump_backtest()
    print(f"{len(paths)} backtest goldens written under go/testdata/golden/{LANE}/")
