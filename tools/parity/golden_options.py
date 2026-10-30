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
* ``ledger.journal_figures``: a closed session's journal row from its fills (OP11);
* ``scan.scan_all`` and ``plan.decide_o1`` / ``plan_o2.decide_o2`` / ``plan_o3.decide_o3`` (each
  with its ``finalize_*``) over ``tests/options_scan_fixtures.py``'s days, the market trimmed to
  the bars started by ``now`` and the quoted contracts (outputs unchanged by the trim).

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


# --- the scan and the three plan builders: inputs back from JSON --------------------------------


def _dt(value: str | None) -> dt.datetime | None:
    return None if value is None else dt.datetime.fromisoformat(value)


def _rebuild(cls: Any, raw: dict[str, Any]) -> Any:
    """A nested config dataclass back from its JSON, each field by the type of its default."""
    from enum import Enum

    default = cls()
    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(cls):
        current, value = getattr(default, f.name), raw[f.name]
        if value is None:
            kwargs[f.name] = None
        elif dataclasses.is_dataclass(current):
            kwargs[f.name] = _rebuild(type(current), value)
        elif isinstance(current, Enum):
            kwargs[f.name] = type(current)(value)
        elif isinstance(current, Decimal):
            kwargs[f.name] = Decimal(str(value))
        elif isinstance(current, dt.time):
            kwargs[f.name] = dt.time.fromisoformat(value)
        elif isinstance(current, dt.date):
            kwargs[f.name] = dt.date.fromisoformat(value)
        elif isinstance(current, tuple):
            kwargs[f.name] = tuple(value)
        elif isinstance(current, (bool, int, float, str)):
            kwargs[f.name] = type(current)(value)
        else:
            raise TypeError(f"{cls.__name__}.{f.name}: no JSON rule for {type(current).__name__}")
    return cls(**kwargs)


def _market(raw: dict[str, Any]) -> Any:
    from baskfy_core.options.bars import Bar
    from baskfy_core.options.calendar import Contract
    from baskfy_core.options.config import OptionType
    from baskfy_core.options.scan import MarketDay

    contracts = tuple(
        Contract(int(c["instrument_token"]), c["tradingsymbol"], c["underlying"],
                 dt.date.fromisoformat(c["expiry"]), Decimal(c["strike"]),
                 OptionType(c["option_type"]), int(c["lot_size"]), Decimal(c["tick_size"]))
        for c in raw["contracts"]
    )  # fmt: skip
    bars = tuple(
        Bar(dt.datetime.fromisoformat(b["ts"]), Decimal(b["open"]), Decimal(b["high"]),
            Decimal(b["low"]), Decimal(b["close"]))
        for b in raw["bars"]
    )  # fmt: skip
    return MarketDay(
        trade_date=dt.date.fromisoformat(raw["trade_date"]),
        trading_day=bool(raw["trading_day"]),
        contracts=contracts,
        bars=bars,
        daily_closes=tuple(Decimal(c) for c in raw["daily_closes"]),
        vix_prev_close=_d(raw["vix_prev_close"]),
    )


def _snapshot(raw: dict[str, Any] | None) -> Any:
    from baskfy_core.options.chain import Level, OptionQuote
    from baskfy_core.options.config import OptionType
    from baskfy_core.options.scan import Snapshot

    if raw is None:
        return None
    ladder = lambda levels: tuple(Level(Decimal(lv["price"]), int(lv["quantity"]))  # noqa: E731
                                  for lv in levels)  # fmt: skip
    quotes = tuple(
        OptionQuote(
            instrument_token=int(q["instrument_token"]), expiry=dt.date.fromisoformat(q["expiry"]),
            strike=Decimal(q["strike"]), option_type=OptionType(q["option_type"]),
            bid=_d(q["bid"]), ask=_d(q["ask"]), bids=ladder(q["bids"]), asks=ladder(q["asks"]),
            oi=int(q["oi"]), ts=dt.datetime.fromisoformat(q["ts"]), last=_d(q["last"]),
            volume=int(q["volume"]),
        )
        for q in raw["quotes"]
    )  # fmt: skip
    return Snapshot(ts=dt.datetime.fromisoformat(raw["ts"]), spot=_d(raw["spot"]), quotes=quotes)


def _book(raw: list[dict[str, Any]]) -> Any:
    from baskfy_core.options.scan import SnapshotBook

    return SnapshotBook({_dt(e["minute"]): _snapshot(e["snapshot"]) for e in raw})


def _context(raw: dict[str, Any]) -> Any:
    from baskfy_core.options.config import Mode, Sleeve
    from baskfy_core.options.scan import DayContext, SleeveContext
    from baskfy_core.options.session import SessionState
    from baskfy_core.options.structures import SleeveBook

    sleeves = {}
    for code, ctx in raw["sleeves"].items():
        b = ctx["book"]
        book = SleeveBook(
            mode=Mode(b["mode"]), sleeve_capital_inr=Decimal(b["sleeve_capital_inr"]),
            risk_per_trade_pct=_d(b["risk_per_trade_pct"]), max_lots=b["max_lots"],
            real_journal_rows=int(b["real_journal_rows"]),
        )  # fmt: skip
        state = ctx["session_state"]
        sleeves[Sleeve(code)] = SleeveContext(
            book=book, paused=bool(ctx["paused"]),
            session_state=None if state is None else SessionState(state),
        )  # fmt: skip
    holder = raw["slot_holder"]
    return DayContext(
        event_days=frozenset(dt.date.fromisoformat(d) for d in raw["event_days"]),
        slot_holder=None if holder is None else Sleeve(holder),
        sleeves=sleeves,
    )


def _rules(inputs: dict[str, Any]) -> tuple[Any, Any]:
    from baskfy_core.options.config import OptionsCeilings, OptionsConfig

    return _rebuild(OptionsConfig, inputs["options"]), _rebuild(OptionsCeilings, inputs["ceilings"])


def _margin(raw: dict[str, Any] | None) -> Any:
    from baskfy_core.options.plan import MarginQuote

    if raw is None:
        return None
    return MarginQuote(Decimal(raw["hedged_inr"]), _d(raw["transient_inr"]))


# --- the scan and the three plan builders: outputs ----------------------------------------------


def _decision_json(decision: Any) -> dict[str, Any]:
    from baskfy_core.options.structures import to_json

    out = {f.name: _prep(getattr(decision, f.name)) for f in dataclasses.fields(decision)}
    out["candidate"] = None if decision.candidate is None else to_json(decision.candidate)
    return out


def _outcome_json(outcome: Any) -> dict[str, Any]:
    return {
        "state": outcome.state,
        "reasons": list(outcome.reasons),
        "plan": None if outcome.plan is None else _prep(outcome.plan),
        "decision_state": outcome.decision.state,
        "decision_reasons": list(outcome.decision.reasons),
        "decision_numbers": outcome.decision.numbers,
    }


def call_scan_all(inputs: dict[str, Any]) -> Any:
    from baskfy_core.options.scan import scan_all

    options, ceilings = _rules(inputs)
    rows = scan_all(_market(inputs["market"]), _context(inputs["context"]),
                    dt.datetime.fromisoformat(inputs["now"]), _book(inputs["snapshots"]),
                    options, ceilings)  # fmt: skip
    return [
        {"sleeve": r.sleeve, "ts": r.ts, "state": r.state, "reasons": list(r.reasons),
         "numbers": r.numbers, "candidates": r.candidates_json(), "as_of_minute": r.as_of_minute,
         "stale": r.stale}
        for r in rows
    ]  # fmt: skip


def call_decide_o1(inputs: dict[str, Any]) -> Any:
    from baskfy_core.options.config import Mode, Sleeve
    from baskfy_core.options.plan import decide_o1, finalize_o1

    options, ceilings = _rules(inputs)
    decision = decide_o1(
        Sleeve(inputs["sleeve"]), _market(inputs["market"]), _context(inputs["context"]),
        _snapshot(inputs["snapshot"]), dt.datetime.fromisoformat(inputs["now"]),
        options=options, ceilings=ceilings,
    )  # fmt: skip
    outcome = finalize_o1(
        decision, _margin(inputs["margin"]), user_id=int(inputs["user_id"]),
        mode=Mode(inputs["mode"]), margin_pool_inr=Decimal(inputs["margin_pool_inr"]),
        margin_in_use_inr=Decimal(inputs["margin_in_use_inr"]), options=options,
    )  # fmt: skip
    return {"decision": _decision_json(decision), "outcome": _outcome_json(outcome)}


def call_decide_o2(inputs: dict[str, Any]) -> Any:
    from baskfy_core.options.config import Mode
    from baskfy_core.options.plan_o2 import decide_o2, finalize_o2

    options, ceilings = _rules(inputs)
    decision = decide_o2(
        _market(inputs["market"]), _context(inputs["context"]), _book(inputs["snapshots"]),
        dt.datetime.fromisoformat(inputs["now"]), options=options, ceilings=ceilings,
    )  # fmt: skip
    outcome = finalize_o2(decision, user_id=int(inputs["user_id"]), mode=Mode(inputs["mode"]),
                          options=options)  # fmt: skip
    return {"decision": _decision_json(decision), "outcome": _outcome_json(outcome)}


def call_decide_o3(inputs: dict[str, Any]) -> Any:
    from baskfy_core.options.config import Mode, Sleeve
    from baskfy_core.options.plan_o3 import decide_o3, finalize_o3

    options, ceilings = _rules(inputs)
    decision = decide_o3(
        Sleeve(inputs["sleeve"]), _market(inputs["market"]), _context(inputs["context"]),
        _book(inputs["snapshots"]), dt.datetime.fromisoformat(inputs["now"]),
        options=options, ceilings=ceilings,
    )  # fmt: skip
    outcome = finalize_o3(
        decision, _margin(inputs["margin"]), user_id=int(inputs["user_id"]),
        mode=Mode(inputs["mode"]), margin_pool_inr=Decimal(inputs["margin_pool_inr"]),
        margin_in_use_inr=Decimal(inputs["margin_in_use_inr"]), options=options,
    )  # fmt: skip
    return {"decision": _decision_json(decision), "outcome": _outcome_json(outcome)}


# --- the backtest, its suite, the replay day and the executor (RS7 C2) ---------------------------


def _outcome(raw: dict[str, Any]) -> Any:
    from baskfy_core.options.backtest import DayOutcome

    t = raw["trigger_time"]
    return DayOutcome(
        trade_date=dt.date.fromisoformat(raw["trade_date"]), traded=bool(raw["traded"]),
        skip_reason=raw["skip_reason"], net_pnl_inr=_d(raw["net_pnl_inr"]),
        r_multiple=_d(raw["r_multiple"]), closed_reason=raw["closed_reason"],
        trigger_time=None if t is None else dt.time.fromisoformat(t),
        mfe_points=_d(raw["mfe_points"]), mae_points=_d(raw["mae_points"]),
    )  # fmt: skip


def call_backtest_run(inputs: dict[str, Any]) -> Any:
    from baskfy_core.options.backtest import Tier, run
    from baskfy_core.options.config import Sleeve

    days = [_outcome(o) for o in inputs["outcomes"]]
    return run(Sleeve(inputs["sleeve"]), Tier(inputs["tier"]), days, lambda o: o,
               min_sessions=int(inputs["min_sessions"]))  # fmt: skip


def call_tier2_quote(inputs: dict[str, Any]) -> Any:
    from baskfy_core.options.backtest import tier2_quote
    from baskfy_core.options.config import OptionType

    return tier2_quote(
        Decimal(inputs["spot"]), Decimal(inputs["strike"]), years=float(inputs["years"]),
        vix_close=Decimal(inputs["vix_close"]), kind=OptionType(inputs["kind"]),
        rate=float(inputs["rate"]), rates=_rates(inputs["rates"]),
    )  # fmt: skip


class _ScriptVenue:
    """A venue over stored books: each send fills ``min(next scripted, asked)`` (all once the
    script runs out) at the attempt's own price."""

    def __init__(self, books: dict[str, Any], script: list[int]) -> None:
        self.books = books
        self.script = list(script)

    def quote(self, role: Any) -> Any:
        from baskfy_core.options.executor import Book

        b = self.books[role.value]
        return Book(Decimal(b["bid"]), Decimal(b["ask"]))

    def send(self, role: Any, attempt: Any, quantity: int, closing: bool) -> Any:
        from baskfy_core.options.executor import Fill

        del role, closing
        filled = min(self.script.pop(0), quantity) if self.script else quantity
        return Fill(filled, attempt.price if filled else None)


def _exec_args(inputs: dict[str, Any]) -> dict[str, Any]:
    from baskfy_core.options.config import ExecutionConfig, Sleeve

    return {"sleeve": Sleeve(inputs["sleeve"]), "tick": Decimal(inputs["tick"]),
            "config": _config(ExecutionConfig, inputs["config"])}  # fmt: skip


def call_run_entry(inputs: dict[str, Any]) -> Any:
    from baskfy_core.options.execution import LegRole
    from baskfy_core.options.executor import Outcome, run_entry, run_exit

    venue = _ScriptVenue(inputs["books"], inputs["script"])
    args = _exec_args(inputs)
    entry = run_entry(venue, [LegRole(r) for r in inputs["roles"]], int(inputs["quantity"]),
                      **args)  # fmt: skip
    exit_ = run_exit(venue, dict(entry.position), **args) if entry.outcome is Outcome.OPEN else None
    return {"entry": entry, "exit": exit_}


def call_run_exit(inputs: dict[str, Any]) -> Any:
    from baskfy_core.options.execution import LegRole
    from baskfy_core.options.executor import run_exit

    venue = _ScriptVenue(inputs["books"], inputs["script"])
    position = {LegRole(k): int(v) for k, v in inputs["position"].items()}
    return run_exit(venue, position, **_exec_args(inputs))


def _source(raw: dict[str, Any], market: Any, options: Any) -> Any:
    from baskfy_core.options.replay_day import ModelChain, StoredChain

    if raw["kind"] == "model":
        return ModelChain(market, options)
    return StoredChain({dt.datetime.fromisoformat(e["minute"]): _snapshot(e["snapshot"])
                        for e in raw["minutes"]})  # fmt: skip


def call_tier1_day(inputs: dict[str, Any]) -> Any:
    from baskfy_core.options.config import Sleeve
    from baskfy_core.options.replay_day import tier1_day

    options, _ceilings = _rules(inputs)
    market, context = _market(inputs["market"]), _context(inputs["context"])
    return {s: tier1_day(Sleeve(s), market, context, options) for s in inputs["sleeves"]}


def call_priced_day(inputs: dict[str, Any]) -> Any:
    from baskfy_core.options.config import Sleeve
    from baskfy_core.options.replay_day import priced_day

    options, ceilings = _rules(inputs)
    market = _market(inputs["market"])
    return priced_day(Sleeve(inputs["sleeve"]), market, _context(inputs["context"]),
                      _source(inputs["source"], market, options), options=options,
                      ceilings=ceilings)  # fmt: skip


def call_run_tier(inputs: dict[str, Any]) -> Any:
    from baskfy_core.options.backtest import Tier
    from baskfy_core.options.backtest_suite import run_tier
    from baskfy_core.options.config import Sleeve

    options, ceilings = _rules(inputs)
    days = [_market(m) for m in inputs["days"]]
    raw = inputs["source"]
    source_for = None if raw is None else (lambda m: _source(raw, m, options))
    return [run_tier(Sleeve(s), Tier(inputs["tier"]), days, context=_context(inputs["context"]),
                     options=options, ceilings=ceilings, source_for=source_for)
            for s in inputs["sleeves"]]  # fmt: skip


def call_tier1_sensitivity(inputs: dict[str, Any]) -> Any:
    from baskfy_core.options.backtest_suite import tier1_sensitivity
    from baskfy_core.options.config import Sleeve

    options, _ceilings = _rules(inputs)
    days = [_market(m) for m in inputs["days"]]
    return {s: tier1_sensitivity(Sleeve(s), days, context=_context(inputs["context"]),
                                 options=options)
            for s in inputs["sleeves"]}  # fmt: skip


CALLS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "baskfy_core.options.gating.options_gates": call_options_gates,
    "baskfy_core.options.costs.charges": call_charges,
    "baskfy_core.options.execution.simulate_fill": call_simulate_fill,
    "baskfy_core.options.execution.never_naked": call_never_naked,
    "baskfy_core.options.condor.exit_decision": call_condor_exit,
    "baskfy_core.options.ledger.journal_figures": call_journal_figures,
    "baskfy_core.options.scan.scan_all": call_scan_all,
    "baskfy_core.options.plan.decide_o1": call_decide_o1,
    "baskfy_core.options.plan_o2.decide_o2": call_decide_o2,
    "baskfy_core.options.plan_o3.decide_o3": call_decide_o3,
    "baskfy_core.options.backtest.run": lambda i: _prep(call_backtest_run(i)),
    "baskfy_core.options.backtest.tier2_quote": lambda i: _prep(call_tier2_quote(i)),
    "baskfy_core.options.executor.run_entry": lambda i: _prep(call_run_entry(i)),
    "baskfy_core.options.executor.run_exit": lambda i: _prep(call_run_exit(i)),
    "baskfy_core.options.replay_day.tier1_day": lambda i: _prep(call_tier1_day(i)),
    "baskfy_core.options.replay_day.priced_day": lambda i: _prep(call_priced_day(i)),
    "baskfy_core.options.backtest_suite.run_tier": lambda i: _prep(call_run_tier(i)),
    "baskfy_core.options.backtest_suite.tier1_sensitivity": lambda i: _prep(call_tier1_sensitivity(i)),
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
    _scan_plan_cases(add)
    _backtest_cases(add)
    return out


# --- the scan and the three plan builders: the cases ---------------------------------------------


def _fixtures() -> Any:
    """``packages/core/tests``' fixture days (case building only; a recompute reads the JSON)."""
    tests = HERE.parents[1] / "decile-blueprint" / "packages" / "core" / "tests"
    if str(tests) not in sys.path:
        sys.path.insert(0, str(tests))
    import options_scan_fixtures

    return options_scan_fixtures


_CALENDAR_STRIKE = Decimal(25000)


def _market_json(market: Any, now: dt.datetime, snaps: list[Any]) -> dict[str, Any]:
    """The day as stored by ``now``: bars started by then; the master's rows at the quoted
    contracts, plus the 25,000 strike of every expiry (the calendar reads only expiries)."""
    quoted = {(q.expiry, q.strike) for s in snaps if s is not None for q in s.quotes}
    return _jsonable(dataclasses.replace(
        market,
        bars=tuple(b for b in market.bars if b.ts <= now),
        contracts=tuple(c for c in market.contracts
                        if c.strike == _CALENDAR_STRIKE or (c.expiry, c.strike) in quoted),
    ))  # fmt: skip


def _book_json(loaded: dict[dt.datetime | None, Any]) -> list[dict[str, Any]]:
    keys = sorted(loaded, key=lambda k: (k is not None, k.isoformat() if k else ""))
    return [{"minute": None if k is None else k.isoformat(), "snapshot": _jsonable(loaded[k])}
            for k in keys]  # fmt: skip


def _context_json(ctx: Any) -> dict[str, Any]:
    return {"event_days": sorted(d.isoformat() for d in ctx.event_days),
            "slot_holder": ctx.slot_holder,
            "sleeves": {str(k): _jsonable(v) for k, v in ctx.sleeves.items()}}  # fmt: skip


def _scan_plan_cases(add: Callable[[str, dict[str, Any]], None]) -> None:  # noqa: PLR0915 - one list of cases
    from baskfy_core.options import plan_o2, plan_o3, scan
    from baskfy_core.options.bars import closed
    from baskfy_core.options.config import Mode, OptionsCeilings, OptionsConfig, Sleeve
    from baskfy_core.options.plan import MarginQuote
    from baskfy_core.options.scan import DayContext, SleeveContext
    from baskfy_core.options.session import SessionState
    from baskfy_core.options.structures import SleeveBook

    fx = _fixtures()
    options, ceilings = OptionsConfig(), OptionsCeilings()
    rules = {"options": _jsonable(options), "ceilings": _jsonable(ceilings)}
    quiet = fx.market(fx.QUIET_MONTHLY, fx.quiet_monthly_bars())
    trend = fx.market(fx.TREND_WEEKLY, fx.trend_weekly_bars())
    gap = fx.market(fx.GAP_HOLD, fx.gap_hold_bars())
    up = fx.market(fx.O2_UP_BREAK, fx.o2_up_break_bars())
    counter = fx.market(fx.O2_COUNTER, fx.o2_counter_bars())
    smiles = {fx.QUIET_MONTHLY: fx.skew(0.30, 0.33), fx.TREND_WEEKLY: fx.flat(0.14),
              fx.GAP_HOLD: fx.flat(0.14), fx.O2_UP_BREAK: fx.flat(0.13),
              fx.O2_COUNTER: fx.flat(0.13)}  # fmt: skip

    def chain(m: Any, minute: dt.datetime) -> Any:
        seen = closed(m.bars, minute)
        if not seen:
            return None
        spot = seen[-1].close
        exps = sorted({c.expiry for c in fx.MASTER if c.expiry >= minute.date()})[:2]
        fwd = {e: fx.carry_forward(spot, minute, e) for e in exps}
        return fx.snapshot(minute, spot, exps, smiles[m.trade_date], forwards=fwd)

    def at(m: Any, hh: int, mm: int, ss: int = 20) -> dt.datetime:
        return fx.at(m.trade_date, hh, mm) + dt.timedelta(seconds=ss)

    def ctx(sleeve: Any = None, **kw: Any) -> Any:
        holder = kw.pop("slot_holder", None)
        events = kw.pop("event_days", frozenset())
        sleeves = {sleeve: SleeveContext(**kw)} if sleeve is not None else {}
        return DayContext(event_days=events, slot_holder=holder, sleeves=sleeves)

    capital = SleeveBook(mode=Mode.LIVE, sleeve_capital_inr=Decimal("2000000"),
                         real_journal_rows=10)  # fmt: skip
    probe = MarginQuote(Decimal("130903.05"), Decimal("98250.00"))

    def load(wanted: Any, m: Any, now: dt.datetime, *, drop: bool, lag: int) -> dict[Any, Any]:
        minute = now.replace(second=0, microsecond=0)
        if drop:
            return {}
        return {w: chain(m, (w or minute) - dt.timedelta(minutes=lag)) for w in wanted}

    # scan_all
    scan_fn = "baskfy_core.options.scan.scan_all"
    for m, now, context, drop in (
        (quiet, at(quiet, 9, 50), DayContext(), False),
        (quiet, at(quiet, 10, 1), DayContext(), False),
        (quiet, at(quiet, 10, 15), DayContext(), False),
        (quiet, at(quiet, 10, 1), ctx(Sleeve.O1M, paused=True), False),
        (quiet, at(quiet, 10, 1), ctx(slot_holder=Sleeve.O3A), False),
        (quiet, at(quiet, 10, 1), ctx(Sleeve.O1M, session_state=SessionState.PLANNED), False),
        (quiet, at(quiet, 10, 1), ctx(event_days=frozenset({fx.QUIET_MONTHLY})), False),
        (quiet, at(quiet, 10, 1), DayContext(), True),
        (quiet, at(quiet, 10, 1), ctx(Sleeve.O1M, book=capital), False),
        (trend, at(trend, 9, 46), DayContext(), False),
        (trend, at(trend, 10, 5), DayContext(), False),
        (trend, at(trend, 10, 21), DayContext(), False),
        (gap, at(gap, 9, 30), DayContext(), False),
        (gap, at(gap, 9, 46), DayContext(), False),
        (gap, at(gap, 10, 30), ctx(Sleeve.O3B, session_state=SessionState.PLANNED), False),
        (up, at(up, 9, 20), DayContext(), False),
        (up, at(up, 9, 45), DayContext(), False),
        (up, at(up, 10, 6), DayContext(), False),
        (up, at(up, 10, 6), ctx(Sleeve.O2, book=capital), False),
        (counter, at(counter, 10, 30), DayContext(), False),
        (counter, at(counter, 13, 33), DayContext(), False),
    ):
        wanted = scan.wanted_minutes(m, context, now, options, ceilings)
        loaded = load(wanted, m, now, drop=drop, lag=0)
        add(scan_fn, {"market": _market_json(m, now, list(loaded.values())),
                      "context": _context_json(context), "snapshots": _book_json(loaded),
                      "now": now.isoformat(), **rules})  # fmt: skip

    def broker(margin: Any, mode: Mode, pool: str = "0", in_use: str = "0") -> dict[str, Any]:
        return {"margin": _jsonable(margin), "user_id": 7, "mode": mode.value,
                "margin_pool_inr": pool, "margin_in_use_inr": in_use}  # fmt: skip

    # decide_o1 + finalize_o1
    o1 = "baskfy_core.options.plan.decide_o1"
    no_closes = dataclasses.replace(quiet, daily_closes=())
    for sleeve, m, now, minute, context, b in (
        (Sleeve.O1M, quiet, at(quiet, 10, 0, 40), (10, 0), DayContext(), broker(probe, Mode.PAPER)),
        (Sleeve.O1M, quiet, at(quiet, 10, 0, 40), (10, 0), DayContext(), broker(None, Mode.PAPER)),
        (Sleeve.O1M, quiet, at(quiet, 10, 0, 40), (10, 0), DayContext(), broker(None, Mode.LIVE)),
        (Sleeve.O1M, quiet, at(quiet, 10, 0, 40), (10, 0), ctx(Sleeve.O1M, book=capital),
         broker(probe, Mode.LIVE, "500000", "100000")),
        (Sleeve.O1M, quiet, at(quiet, 10, 0, 40), (10, 0), ctx(Sleeve.O1M, book=capital),
         broker(probe, Mode.LIVE, "120000", "0")),
        (Sleeve.O1M, quiet, at(quiet, 10, 0, 40), (10, 0), DayContext(),
         broker(probe, Mode.PAPER, "300000", "50000")),
        (Sleeve.O1M, quiet, at(quiet, 9, 50), None, DayContext(), broker(probe, Mode.PAPER)),
        (Sleeve.O1M, quiet, at(quiet, 10, 15, 30), (10, 0), DayContext(),
         broker(probe, Mode.PAPER)),
        (Sleeve.O1M, quiet, at(quiet, 10, 0, 40), None, DayContext(), broker(probe, Mode.PAPER)),
        (Sleeve.O1M, quiet, at(quiet, 10, 0, 40), (10, 0),
         ctx(event_days=frozenset({fx.QUIET_MONTHLY})), broker(probe, Mode.PAPER)),
        (Sleeve.O1W, quiet, at(quiet, 10, 0, 40), (10, 0), DayContext(), broker(probe, Mode.PAPER)),
        (Sleeve.O1W, trend, at(trend, 10, 0, 40), (10, 0), DayContext(), broker(probe, Mode.PAPER)),
        (Sleeve.O1W, gap, at(gap, 10, 0, 40), (10, 0), DayContext(), broker(probe, Mode.PAPER)),
        (Sleeve.O1M, quiet, at(quiet, 10, 0, 40), (10, 0), ctx(Sleeve.O1M, paused=True),
         broker(probe, Mode.PAPER)),
        (Sleeve.O1M, quiet, at(quiet, 10, 0, 40), (10, 0), ctx(slot_holder=Sleeve.O3B),
         broker(probe, Mode.PAPER)),
        (Sleeve.O1M, quiet, at(quiet, 10, 2, 30), (10, 0), DayContext(), broker(probe, Mode.PAPER)),
        (Sleeve.O1M, no_closes, at(quiet, 10, 0, 40), (10, 0), DayContext(),
         broker(probe, Mode.PAPER)),
    ):
        snap = None if minute is None else chain(m, fx.at(m.trade_date, *minute))
        add(o1, {"sleeve": sleeve.value, "market": _market_json(m, now, [snap]),
                 "context": _context_json(context), "snapshot": _jsonable(snap),
                 "now": now.isoformat(), **rules, **b})  # fmt: skip

    # decide_o2 + finalize_o2
    o2 = "baskfy_core.options.plan_o2.decide_o2"
    vix_high = dataclasses.replace(up, vix_prev_close=Decimal("25"))
    falling = dataclasses.replace(up, daily_closes=fx.falling_closes())
    for m, now, context, mode, drop in (
        (up, at(up, 10, 5, 45), DayContext(), Mode.PAPER, False),
        (up, at(up, 10, 5, 45), ctx(Sleeve.O2, book=capital), Mode.LIVE, False),
        (up, at(up, 10, 5, 45), DayContext(), Mode.PAPER, True),
        (up, at(up, 9, 20), DayContext(), Mode.PAPER, False),
        (up, at(up, 9, 45), DayContext(), Mode.PAPER, False),
        (up, at(up, 13, 40), DayContext(), Mode.PAPER, False),
        (up, at(up, 10, 5, 45), ctx(Sleeve.O2, paused=True), Mode.PAPER, False),
        (up, at(up, 10, 5, 45), ctx(event_days=frozenset({fx.O2_UP_BREAK})), Mode.PAPER, False),
        (vix_high, at(up, 10, 5, 45), DayContext(), Mode.PAPER, False),
        (falling, at(up, 10, 5, 45), DayContext(), Mode.PAPER, False),
        (counter, at(counter, 10, 30), DayContext(), Mode.PAPER, False),
        (counter, at(counter, 13, 33), DayContext(), Mode.PAPER, False),
        (trend, at(trend, 9, 40), DayContext(), Mode.PAPER, False),
        (trend, at(trend, 10, 30), DayContext(), Mode.PAPER, False),
    ):
        wanted = plan_o2.wanted_minutes(m, context, now, options, ceilings)
        loaded = load(wanted, m, now, drop=drop, lag=0)
        add(o2, {"market": _market_json(m, now, list(loaded.values())),
                 "context": _context_json(context), "snapshots": _book_json(loaded),
                 "now": now.isoformat(), **rules, "user_id": 7, "mode": mode.value})  # fmt: skip

    # decide_o3 + finalize_o3
    o3 = "baskfy_core.options.plan_o3.decide_o3"
    for sleeve, m, now, context, b, drop in (
        (Sleeve.O3A, trend, at(trend, 10, 20, 20), DayContext(), broker(None, Mode.PAPER), False),
        (Sleeve.O3A, trend, at(trend, 10, 20, 20), DayContext(),
         broker(MarginQuote(Decimal("41250.40"), None), Mode.PAPER, "200000", "0"), False),
        (Sleeve.O3A, trend, at(trend, 10, 20, 20), DayContext(), broker(None, Mode.LIVE), False),
        (Sleeve.O3A, trend, at(trend, 10, 20, 20), ctx(Sleeve.O3A, book=capital),
         broker(MarginQuote(Decimal("41250.40"), None), Mode.LIVE, "200000", "20000"), False),
        (Sleeve.O3B, gap, at(gap, 9, 45, 20), DayContext(), broker(None, Mode.PAPER), False),
        (Sleeve.O3B, trend, at(trend, 9, 45, 20), DayContext(), broker(None, Mode.PAPER), False),
        (Sleeve.O3A, gap, at(gap, 10, 20, 20),
         ctx(Sleeve.O3B, session_state=SessionState.PLANNED), broker(None, Mode.PAPER), False),
        (Sleeve.O3A, trend, at(trend, 10, 0), DayContext(), broker(None, Mode.PAPER), False),
        (Sleeve.O3B, gap, at(gap, 10, 30), DayContext(), broker(None, Mode.PAPER), False),
        (Sleeve.O3B, gap, at(gap, 9, 45, 20), ctx(slot_holder=Sleeve.O1W),
         broker(None, Mode.PAPER), False),
        (Sleeve.O3B, gap, at(gap, 9, 45, 20), ctx(Sleeve.O3B, paused=True),
         broker(None, Mode.PAPER), False),
        (Sleeve.O3B, gap, at(gap, 9, 45, 20), ctx(event_days=frozenset({fx.GAP_HOLD})),
         broker(None, Mode.PAPER), False),
        (Sleeve.O3A, up, at(up, 10, 20, 20), DayContext(), broker(None, Mode.PAPER), False),
        (Sleeve.O3B, gap, at(gap, 9, 45, 20), DayContext(), broker(None, Mode.PAPER), True),
    ):
        wanted = plan_o3.wanted_minutes(sleeve, m, context, now, options, ceilings)
        loaded = load(wanted, m, now, drop=drop, lag=0)
        add(o3, {"sleeve": sleeve.value,
                 "market": _market_json(m, now, list(loaded.values())),
                 "context": _context_json(context), "snapshots": _book_json(loaded),
                 "now": now.isoformat(), **rules, **b})  # fmt: skip


# --- the backtest, its suite, the replay day and the executor: the cases ------------------------


def _day_json(market: Any, *, expiries: int) -> dict[str, Any]:
    """A whole day: every bar; the master's rows of the day's ``expiries`` nearest expiries plus
    the 25,000 strike of every expiry (the calendar reads only expiries)."""
    near = sorted({c.expiry for c in market.contracts if c.expiry >= market.trade_date})[:expiries]
    return _jsonable(dataclasses.replace(
        market,
        contracts=tuple(c for c in market.contracts
                        if c.strike == _CALENDAR_STRIKE or c.expiry in near),
    ))  # fmt: skip


def _backtest_cases(add: Callable[[str, dict[str, Any]], None]) -> None:  # noqa: PLR0915 - one list of cases
    import random

    from baskfy_core.options.bars import closed
    from baskfy_core.options.chain import Level
    from baskfy_core.options.config import OptionsCeilings, OptionsConfig, Sleeve
    from baskfy_core.options.scan import DayContext

    fx = _fixtures()
    options, ceilings = OptionsConfig(), OptionsCeilings()
    rules = {"options": _jsonable(options), "ceilings": _jsonable(ceilings)}
    no_ctx = _context_json(DayContext())

    # backtest.run
    run_fn = "baskfy_core.options.backtest.run"
    day = lambda d, **kw: {"trade_date": d, "traded": False, "skip_reason": None,  # noqa: E731
                           "net_pnl_inr": None, "r_multiple": None, "closed_reason": None,
                           "trigger_time": None, "mfe_points": None, "mae_points": None, **kw}  # fmt: skip
    priced = [
        day("2026-01-20", traded=True, net_pnl_inr="1500", r_multiple="1.5",
            closed_reason="TARGET"),
        day("2025-12-30", traded=True, net_pnl_inr="500", r_multiple="0.5"),
        day("2026-01-06", skip_reason="ER_TOO_HIGH"),
        day("2026-01-13", traded=True, net_pnl_inr="-1000", r_multiple="-1"),
        day("2026-01-27", skip_reason="ER_TOO_HIGH"),
        day("2026-02-03", skip_reason=""),
        day("2026-02-10", skip_reason="GAP_TOO_LARGE"),
        day("2026-02-17", traded=True, net_pnl_inr="0.00", r_multiple="0.00"),
        day("2026-02-24", traded=True, net_pnl_inr="-412.35", r_multiple="-0.41"),
    ]  # fmt: skip
    signals = [
        day("2026-10-20", traded=True, trigger_time="10:19:00", mfe_points="80.5",
            mae_points="-12.25"),
        day("2026-10-13", traded=True, trigger_time="09:45:00", mfe_points="0", mae_points="0"),
        day("2026-10-27", skip_reason="NOT_CONTAINED"),
        day("2025-10-28", skip_reason="EVENT_DAY"),
    ]  # fmt: skip
    for sleeve, tier, outcomes, n in (
        ("O1W", "2", priced, 20), ("O2", "2", priced, 60), ("O1M", "3", priced, 12),
        ("O3A", "3", priced[:3], 3), ("O2", "1", signals, 60), ("O3B", "1", [], 20),
        ("O1M", "3", [], 12), ("O3A", "2", [priced[2], priced[4]], 20),
        ("O1W", "3", [priced[7], priced[7]], 20),
    ):  # fmt: skip
        add(run_fn, {"sleeve": sleeve, "tier": tier, "outcomes": outcomes, "min_sessions": n})

    # backtest.tier2_quote
    quote_fn = "baskfy_core.options.backtest.tier2_quote"
    rates = _default_rates()
    for spot, strike, years, vix, kind in (
        ("25000", "25100", 7 / 365, "14", "CE"), ("25000", "27000", 1 / 365, "10", "CE"),
        ("25012.35", "24800", 0.0123, "13.45", "PE"), ("25000", "25000", 1e-6, "14", "PE"),
        ("24950.5", "25000", 0.25, "22.8", "CE"), ("25000", "23000", 2 / 365, "15", "PE"),
    ):  # fmt: skip
        add(quote_fn, {"spot": spot, "strike": strike, "years": years, "vix_close": vix,
                       "kind": kind, "rate": 0.065, "rates": rates})  # fmt: skip

    # executor.run_entry / run_exit over scripted fills
    execution = _default_execution()
    books = {"LONG_CALL": {"bid": "10.00", "ask": "10.20"}, "LONG_PUT": {"bid": "8.40", "ask": "8.55"},
             "SHORT_CALL": {"bid": "20.05", "ask": "20.30"},
             "SHORT_PUT": {"bid": "18.90", "ask": "19.10"}}  # fmt: skip
    condor = ["SHORT_CALL", "SHORT_PUT", "LONG_CALL", "LONG_PUT"]
    spread, long_ = ["LONG_CALL", "SHORT_CALL"], ["LONG_CALL"]
    entry_fn = "baskfy_core.options.executor.run_entry"
    base = {"quantity": 65, "tick": "0.05", "config": execution, "books": books}
    for roles, sleeve, script in (
        (condor, "O1M", []), (spread, "O3A", []), (long_, "O2", []),
        (condor, "O1M", [65, 0, 0]), (spread, "O3A", [25, 25]), (spread, "O3A", [65, 40, 25]),
        (long_, "O2", [0, 65, 0, 0, 30]), (condor, "O1W", [65, 65, 65, 65, 0, 0, 65, 10, 55]),
    ):  # fmt: skip
        add(entry_fn, {**base, "roles": roles, "sleeve": sleeve, "script": script})
    for roles, sleeve in ((condor, "O1M"), (spread, "O3A"), (long_, "O2")):
        for seed in range(4):
            rng = random.Random(seed)
            script = [rng.choice([0, 65, rng.randint(0, 65)]) for _ in range(24)]
            add(entry_fn, {**base, "roles": roles, "sleeve": sleeve, "script": script})
    exit_fn = "baskfy_core.options.executor.run_exit"
    for position, sleeve, script in (
        ({"LONG_CALL": 65, "SHORT_CALL": 65}, "O3A", [0, 0]),
        ({"LONG_CALL": 65, "SHORT_CALL": 65}, "O3A", [65, 0, 0]),
        ({"LONG_CALL": 65}, "O2", [0, 0]),
        ({"LONG_CALL": 65, "LONG_PUT": 65, "SHORT_CALL": 65, "SHORT_PUT": 65}, "O1M", [30, 35]),
        ({"LONG_CALL": 50, "LONG_PUT": 0, "SHORT_CALL": 0}, "O1W", []),
        ({}, "O1M", []),
    ):  # fmt: skip
        add(exit_fn, {**base, "position": position, "sleeve": sleeve, "script": script})

    # the fixture days
    quiet = fx.market(fx.QUIET_MONTHLY, fx.quiet_monthly_bars())
    trend = fx.market(fx.TREND_WEEKLY, fx.trend_weekly_bars())
    gap = fx.market(fx.GAP_HOLD, fx.gap_hold_bars())
    up = fx.market(fx.O2_UP_BREAK, fx.o2_up_break_bars())
    counter = fx.market(fx.O2_COUNTER, fx.o2_counter_bars())
    sleeves = [s.value for s in Sleeve]

    # replay_day.tier1_day: every sleeve on each day
    tier1_fn = "baskfy_core.options.replay_day.tier1_day"
    for m, context in (
        (quiet, DayContext()), (trend, DayContext()), (gap, DayContext()), (up, DayContext()),
        (counter, DayContext()), (gap, DayContext(event_days=frozenset({fx.GAP_HOLD}))),
        (dataclasses.replace(trend, daily_closes=()), DayContext()),
        (dataclasses.replace(up, daily_closes=fx.falling_closes()), DayContext()),
        (dataclasses.replace(up, trading_day=False), DayContext()),
    ):  # fmt: skip
        add(tier1_fn, {"sleeves": sleeves, "market": _day_json(m, expiries=0),
                       "context": _context_json(context), **rules})  # fmt: skip

    # replay_day.priced_day: Tier 2 on the model chain, Tier 3 on the planted minutes
    priced_fn = "baskfy_core.options.replay_day.priced_day"
    model = {"kind": "model"}
    for sleeve, m in ((Sleeve.O3A, trend), (Sleeve.O2, up), (Sleeve.O1M, quiet),
                      (Sleeve.O1W, trend), (Sleeve.O3B, gap), (Sleeve.O2, counter),
                      (Sleeve.O1W, quiet), (Sleeve.O3A, up)):  # fmt: skip
        add(priced_fn, {"sleeve": sleeve.value, "market": _day_json(m, expiries=2),
                        "context": no_ctx, "source": model, **rules})  # fmt: skip

    def chain(bars: Any, minute: dt.datetime) -> Any:
        spot = closed(bars, minute)[-1].close
        exps = sorted({c.expiry for c in fx.MASTER if c.expiry >= minute.date()})[:2]
        fwd = {e: fx.carry_forward(spot, minute, e) for e in exps}
        return fx.snapshot(minute, spot, exps, fx.flat(0.14), forwards=fwd)

    def planted(exit_minute: int, long_bid: str, short_ask: str) -> dict[str, Any]:
        decision, entry, exit_ = (fx.at(fx.TREND_WEEKLY, 10, m) for m in (20, 21, exit_minute))
        at_exit = chain(trend.bars, exit_)
        plant = {Decimal(25200): (Decimal(long_bid), Decimal(long_bid) + Decimal("0.20")),
                 Decimal(25300): (Decimal(short_ask) - Decimal("0.20"), Decimal(short_ask))}  # fmt: skip

        def setq(q: Any) -> Any:
            bid, ask = plant[q.strike]
            return dataclasses.replace(q, bid=bid, ask=ask, bids=(Level(bid, 1300),) * 3,
                                       asks=(Level(ask, 1300),) * 3)  # fmt: skip

        weekly = lambda q: q.expiry == fx.TREND_WEEKLY and q.option_type.value == "CE"  # noqa: E731
        quotes = tuple(setq(q) if weekly(q) and q.strike in plant else q for q in at_exit.quotes)
        minutes = {decision: chain(trend.bars, decision), entry: chain(trend.bars, entry),
                   exit_: dataclasses.replace(at_exit, quotes=quotes)}  # fmt: skip
        return {"kind": "stored",
                "minutes": [{"minute": k.isoformat(), "snapshot": _jsonable(v)}
                            for k, v in sorted(minutes.items())]}  # fmt: skip

    target = planted(30, "95.00", "10.00")
    for source in (target, planted(35, "40.00", "30.00"), {"kind": "stored", "minutes": []}):
        add(priced_fn, {"sleeve": "O3A", "market": _day_json(trend, expiries=2),
                        "context": no_ctx, "source": source, **rules})  # fmt: skip

    # backtest_suite.run_tier and tier1_sensitivity
    tier_fn = "baskfy_core.options.backtest_suite.run_tier"
    four = [_day_json(m, expiries=0) for m in (quiet, trend, gap, up)]
    add(tier_fn, {"sleeves": sleeves, "tier": "1", "days": four, "context": no_ctx,
                  "source": None, **rules})  # fmt: skip
    add(tier_fn, {"sleeves": ["O2"], "tier": "2", "days": [_day_json(up, expiries=2)],
                  "context": no_ctx, "source": model, **rules})  # fmt: skip
    add(tier_fn, {"sleeves": ["O3A", "O3B"], "tier": "2",
                  "days": [_day_json(trend, expiries=2), _day_json(gap, expiries=2)],
                  "context": no_ctx, "source": model, **rules})  # fmt: skip
    add(tier_fn, {"sleeves": ["O3A"], "tier": "3", "days": [_day_json(trend, expiries=2)],
                  "context": no_ctx, "source": target, **rules})  # fmt: skip
    add("baskfy_core.options.backtest_suite.tier1_sensitivity",
        {"sleeves": sleeves, "days": four, "context": no_ctx, **rules})


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
