"""The options lane of the Go parity goldens (OP15, ``docs/options/06`` OP15).

    cd decile-blueprint
    uv run python ../tools/parity/golden_options.py

Dumps six pure functions of ``baskfy_core.options`` into
``go/testdata/golden/L1/options/<function>.case_NNN.json``, written ``stable=True`` by
``tools/parity/golden.py``'s own encoder, so two runs are byte-identical:

* ``gating.options_gates``: the four-flag AND, every combination for O2 plus each sleeve all-on;
* ``costs.charges``: ``04`` §6.1's charges over a condor's eight orders, a long's two, a spread's
  four;
* ``execution.simulate_fill``: the paper fill walking a depth ladder to a limit (§8.4);
* ``execution.never_naked``: Track C §2's property;
* ``condor.exit_decision``: the condor's exit precedence, including OP13.2's unknown spot;
* ``ledger.journal_figures``: a closed session's journal row from its fills (OP11).

Every case's inputs are stored as JSON (Decimals as strings, enums as their values, times as
ISO), and each function is called from those stored inputs, never from Python objects that were
not written down. ``packages/core/tests/test_options_goldens.py`` recomputes every file from its
inputs and compares it byte for byte, so a rule change fails there before any Go test reads a stale
golden. The config each case needs is ``OptionsConfig()``'s defaults, written into the inputs.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import importlib.util
import itertools
import sys
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

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


LANE = "L1/options"
NOTES = (
    "Decimals are strings; enums are their values; datetimes carry +05:30. Config is "
    "OptionsConfig() defaults, written into the inputs. Regenerate with "
    "`uv run python ../tools/parity/golden_options.py` from decile-blueprint; "
    "packages/core/tests/test_options_goldens.py recomputes every case from these inputs."
)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _d(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _prep(obj: Any) -> Any:
    """A config as plain values: a ``time`` becomes ``HH:MM:SS`` here, so the shared encoder (and
    the Go testkit that mirrors it) needs no rule for a type only this lane's inputs carry."""
    if isinstance(obj, dt.time):
        return obj.isoformat()
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _prep(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {k: _prep(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_prep(v) for v in obj]
    return obj


def _jsonable(obj: Any) -> Any:
    return _golden()._enc(_prep(obj))


# --- the calls, from stored inputs -------------------------------------------------------------


def call_options_gates(inputs: dict[str, Any]) -> Any:
    from baskfy_core.options.config import Sleeve
    from baskfy_core.options.gating import OptionsFlags, options_gates

    flags = OptionsFlags(**{k: bool(v) for k, v in inputs["flags"].items()})
    return options_gates(Sleeve(inputs["sleeve"]), flags)


def _config(cls: Any, raw: dict[str, Any]) -> Any:
    """A config dataclass back from its JSON, each field by the type of its own default."""
    default = cls()
    kwargs: dict[str, Any] = {}
    for key, value in raw.items():
        current = getattr(default, key)
        if value is None:
            kwargs[key] = None
        elif isinstance(current, Decimal):
            kwargs[key] = Decimal(str(value))
        elif isinstance(current, dt.time):
            kwargs[key] = dt.time.fromisoformat(value)
        elif isinstance(current, dt.date):
            kwargs[key] = dt.date.fromisoformat(value)
        elif isinstance(current, bool):
            kwargs[key] = bool(value)
        elif isinstance(current, int):
            kwargs[key] = int(value)
        else:
            raise TypeError(f"{cls.__name__}.{key}: no JSON rule for {type(current).__name__}")
    return cls(**kwargs)


def _rates(raw: dict[str, Any]) -> Any:
    from baskfy_core.options.config import CostRates

    return _config(CostRates, raw)


def call_charges(inputs: dict[str, Any]) -> Any:
    from baskfy_core.options.config import Side
    from baskfy_core.options.costs import CostFill, charges

    fills = [CostFill(Side(f["side"]), Decimal(f["price"]), int(f["quantity"]))
             for f in inputs["fills"]]  # fmt: skip
    breakdown = charges(fills, _rates(inputs["rates"]))
    return {"breakdown": breakdown, "total": breakdown.total}


def call_simulate_fill(inputs: dict[str, Any]) -> Any:
    from baskfy_core.options.chain import Level
    from baskfy_core.options.config import ExecutionConfig, Side
    from baskfy_core.options.execution import simulate_fill

    return simulate_fill(
        Side(inputs["side"]),
        [Level(Decimal(lv["price"]), int(lv["quantity"])) for lv in inputs["ladder"]],
        int(inputs["quantity"]),
        limit_price=Decimal(inputs["limit_price"]),
        tick=Decimal(inputs["tick"]),
        config=_config(ExecutionConfig, inputs["config"]),
    )


def call_never_naked(inputs: dict[str, Any]) -> Any:
    from baskfy_core.options.execution import LegRole, never_naked

    return never_naked({LegRole(k): int(v) for k, v in inputs["position"].items()})


def call_condor_exit(inputs: dict[str, Any]) -> Any:
    from baskfy_core.options import condor
    from baskfy_core.options.config import OptionsConfig

    options = OptionsConfig()
    cfg = options.condor_monthly if inputs["variant"] == "MONTHLY" else options.condor_weekly
    return condor.exit_decision(
        entry_credit=Decimal(inputs["entry_credit"]), cost_now=Decimal(inputs["cost_now"]),
        spot=_d(inputs["spot"]), short_call_strike=Decimal(inputs["short_call_strike"]),
        short_put_strike=Decimal(inputs["short_put_strike"]),
        now=dt.datetime.fromisoformat(inputs["now"]), stale=bool(inputs["stale"]),
        marked_loss_inr=Decimal(inputs["marked_loss_inr"]),
        risk_budget_inr=Decimal(inputs["risk_budget_inr"]), config=cfg, options=options,
        manual=bool(inputs["manual"]),
    )  # fmt: skip


def call_journal_figures(inputs: dict[str, Any]) -> Any:
    from baskfy_core.options.config import Side
    from baskfy_core.options.ledger import LegFill, journal_figures

    fills = [LegFill(Side(f["side"]), Decimal(f["price"]), int(f["quantity"]), bool(f["closing"]))
             for f in inputs["fills"]]  # fmt: skip
    return journal_figures(
        fills, r_inr=Decimal(inputs["r_inr"]), quantity=int(inputs["quantity"]),
        opened_at=dt.datetime.fromisoformat(inputs["opened_at"]),
        closed_at=dt.datetime.fromisoformat(inputs["closed_at"]),
        peak_points=_d(inputs["peak_points"]), trough_points=_d(inputs["trough_points"]),
        rates=_rates(inputs["rates"]),
    )  # fmt: skip


CALLS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "baskfy_core.options.gating.options_gates": call_options_gates,
    "baskfy_core.options.costs.charges": call_charges,
    "baskfy_core.options.execution.simulate_fill": call_simulate_fill,
    "baskfy_core.options.execution.never_naked": call_never_naked,
    "baskfy_core.options.condor.exit_decision": call_condor_exit,
    "baskfy_core.options.ledger.journal_figures": call_journal_figures,
}


# --- the cases ---------------------------------------------------------------------------------


def _default_rates() -> dict[str, Any]:
    from baskfy_core.options.config import OptionsConfig

    return _jsonable(OptionsConfig().costs)


def _default_execution() -> dict[str, Any]:
    from baskfy_core.options.config import ExecutionConfig

    return _jsonable(ExecutionConfig())


def cases() -> list[tuple[str, str, dict[str, Any]]]:
    """(fn, case file stem, JSON inputs) for every golden, in a fixed order."""
    out: list[tuple[str, str, dict[str, Any]]] = []

    def add(fn: str, inputs: dict[str, Any]) -> None:
        short = fn.rsplit(".", 1)[-1]
        n = sum(1 for f, _, _ in out if f == fn) + 1
        out.append((fn, f"{short}.case_{n:03d}", inputs))

    gates = "baskfy_core.options.gating.options_gates"
    names = ("dry_run", "options_enabled", "intraday_enabled", "o2_execution_enabled")
    for combo in itertools.product([True, False], repeat=4):
        add(gates, {"sleeve": "O2", "flags": dict(zip(names, combo, strict=True))})
    for sleeve, flag in (("O1M", "o1m_execution_enabled"), ("O1W", "o1w_execution_enabled"),
                         ("O3A", "o3_execution_enabled"), ("O3B", "o3_execution_enabled")):  # fmt: skip
        add(gates, {"sleeve": sleeve, "flags": {"dry_run": False, "options_enabled": True,
                                                "intraday_enabled": True, flag: True}})  # fmt: skip

    rates = _default_rates()
    fill = lambda side, price, qty=65: {"side": side, "price": price, "quantity": qty}  # noqa: E731
    charges = "baskfy_core.options.costs.charges"
    add(charges, {"rates": rates, "fills": [
        fill("BUY", "5.10"), fill("BUY", "5.10"), fill("SELL", "20.00"), fill("SELL", "20.00"),
        fill("BUY", "9.85"), fill("BUY", "9.85"), fill("SELL", "1.20"), fill("SELL", "1.20")]})
    add(charges, {"rates": rates, "fills": [fill("BUY", "114.75"), fill("SELL", "186.30")]})
    add(charges, {"rates": rates, "fills": [fill("BUY", "58.40"), fill("SELL", "22.40"),
                                            fill("BUY", "10.00"), fill("SELL", "95.00")]})
    add(charges, {"rates": rates, "fills": []})

    execution = _default_execution()
    level = lambda price, qty: {"price": price, "quantity": qty}  # noqa: E731
    sim = "baskfy_core.options.execution.simulate_fill"
    for side, ladder, qty, limit in (
        ("BUY", [level("35.20", 1300)], 65, "35.25"),
        ("BUY", [level("35.20", 20), level("35.30", 20), level("35.40", 1300)], 65, "35.35"),
        ("BUY", [level("35.20", 20), level("35.30", 20), level("35.40", 1300)], 65, "35.50"),
        ("SELL", [level("20.00", 1300)], 130, "19.95"),
        ("SELL", [level("19.90", 10)], 65, "20.00"),
        ("BUY", [], 65, "10.00"),
    ):
        add(sim, {"side": side, "ladder": ladder, "quantity": qty, "limit_price": limit,
                  "tick": "0.05", "config": execution})  # fmt: skip

    naked = "baskfy_core.options.execution.never_naked"
    for position in (
        {"LONG_CALL": 65, "LONG_PUT": 65, "SHORT_CALL": 65, "SHORT_PUT": 65},
        {"LONG_CALL": 65, "SHORT_CALL": 130},
        {"LONG_PUT": 0, "SHORT_PUT": 65},
        {"LONG_CALL": 65},
        {"SHORT_CALL": 0, "SHORT_PUT": 0},
        {"LONG_CALL": 130, "SHORT_CALL": 65, "LONG_PUT": 65, "SHORT_PUT": 65},
    ):
        add(naked, {"position": position})

    condor_exit = "baskfy_core.options.condor.exit_decision"
    base = {"variant": "MONTHLY", "entry_credit": "40.00", "short_call_strike": "25150",
            "short_put_strike": "24850", "stale": False, "marked_loss_inr": "0",
            "risk_budget_inr": "50000", "manual": False}  # fmt: skip
    at = lambda hh, mm: dt.datetime(2026, 10, 27, hh, mm, tzinfo=IST).isoformat()  # noqa: E731
    for extra in (
        {"cost_now": "16.20", "spot": "25000", "now": at(11, 0)},  # PROFIT
        {"cost_now": "16.20", "spot": "25000", "now": at(11, 0), "stale": True},  # held
        {"cost_now": "30.00", "spot": "25150", "now": at(11, 0)},  # STRIKE_TOUCH
        {"cost_now": "61.00", "spot": "25000", "now": at(11, 0)},  # STOP
        {"cost_now": "30.00", "spot": "25000", "now": at(14, 30)},  # HARD_EXIT
        {"cost_now": "30.00", "spot": None, "now": at(11, 0)},  # OP13.2: held, no spot
        {"cost_now": "61.00", "spot": None, "now": at(11, 0)},  # OP13.2: STOP without a spot
        {"cost_now": "30.00", "spot": "25000", "now": at(11, 0), "manual": True},  # MANUAL
    ):
        add(condor_exit, {**base, **extra})

    journal = "baskfy_core.options.ledger.journal_figures"
    closing = lambda side, price, qty=65: {"side": side, "price": price, "quantity": qty,  # noqa: E731
                                          "closing": True}  # fmt: skip
    opening = lambda side, price, qty=65: {"side": side, "price": price, "quantity": qty,  # noqa: E731
                                          "closing": False}  # fmt: skip
    add(journal, {"fills": [opening("BUY", "5.10"), opening("BUY", "5.10"),
                            opening("SELL", "20.00"), opening("SELL", "20.00"),
                            closing("BUY", "9.85"), closing("BUY", "9.85"),
                            closing("SELL", "1.20"), closing("SELL", "1.20")],
                  "r_inr": "8052.00", "quantity": 65, "opened_at": at(10, 2),
                  "closed_at": at(12, 41), "peak_points": "14.10", "trough_points": "-3.40",
                  "rates": rates})  # fmt: skip
    add(journal, {"fills": [opening("BUY", "114.75"), closing("SELL", "186.30")],
                  "r_inr": "2487.50", "quantity": 65, "opened_at": at(10, 7),
                  "closed_at": at(10, 32), "peak_points": "71.55", "trough_points": None,
                  "rates": rates})  # fmt: skip
    add(journal, {"fills": [opening("BUY", "58.40"), opening("SELL", "22.40"),
                            closing("SELL", "40.10"), closing("BUY", "21.05")],
                  "r_inr": "2840.00", "quantity": 65, "opened_at": at(9, 47),
                  "closed_at": at(13, 19), "peak_points": None, "trough_points": "-17.30",
                  "rates": rates})  # fmt: skip
    return out


def render_case(fn: str, inputs: dict[str, Any]) -> str:
    golden = _golden()
    doc = golden.document(fn=fn, inputs=inputs, output=CALLS[fn](inputs), notes=NOTES, stable=True)
    return golden.render(doc)


def dump_options() -> list[Path]:
    golden = _golden()
    directory = golden.GOLDEN_DIR / LANE
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for fn, stem, inputs in cases():
        path = directory / f"{stem}.json"
        path.write_text(render_case(fn, inputs), encoding="utf-8")
        written.append(path)
    return written


if __name__ == "__main__":
    paths = dump_options()
    print(f"{len(paths)} options goldens written under go/testdata/golden/{LANE}/")
