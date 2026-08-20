"""``factors explain`` (Prompt 5 deliverable 6).

    "A `factors explain --symbol CUPID --date 2026-08-19 --factor sharpe_12m` CLI that prints the
     intermediate series and the final value, for auditability."

docs/02 §"Non-negotiable engineering rules" #4 is the reason it exists: "**Every derived number is
reproducible.** A CLI can recompute any factor for any instrument on any date and print the
intermediate series."

What it prints, and why
-----------------------
Not just the answer — the *inputs to the answer*. For ``sharpe_12m`` that means the window's
resolved start and length, the return, the volatility, and the arithmetic joining them, so a
number a user disputes can be walked back to the bars it came from. A factor engine that can only
say "5.19" is not auditable; one that says "5.19, from these four windows, over these dates, from
this many bars" is.

Usage:
    python -m decile_worker.factors_cli explain --symbol CUPID --date 2026-08-18 \\
        --factor sharpe_12m
    python -m decile_worker.factors_cli explain --symbol CUPID --date 2026-08-18 --factor all
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

import polars as pl
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from decile_core.blends import blend_sql
from decile_core.factor_registry import FACTORS, Factor
from decile_core.models import Instrument
from decile_worker.db import run_in_session
from decile_worker.engine import PolarsFactorEngine, load_history

#: How many rows of the underlying price series to show. Enough to see the shape of the window
#: without burying the answer.
SERIES_PREVIEW: Final = 10


@dataclass(slots=True)
class Explanation:
    symbol: str
    date: dt.date
    factor: str
    value: object = None
    window_lengths: dict[int, int] = field(default_factory=dict)
    inputs: dict[str, object] = field(default_factory=dict)
    series_preview: list[dict[str, object]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_payload(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "date": self.date.isoformat(),
            "factor": self.factor,
            "value": _jsonable(self.value),
            "window_lengths": {str(k): v for k, v in self.window_lengths.items()},
            "inputs": {k: _jsonable(v) for k, v in self.inputs.items()},
            "series_preview": [
                {k: _jsonable(v) for k, v in row.items()} for row in self.series_preview
            ],
            "notes": self.notes,
        }


def _jsonable(value: object) -> object:
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, float | int | str | bool) or value is None:
        return value
    return str(value)


async def explain(session: AsyncSession, symbol: str, on: dt.date, factor_key: str) -> Explanation:
    """Recompute one instrument's factors for one date and lay out the working."""
    instrument_id = (
        await session.execute(select(Instrument.id).where(Instrument.symbol == symbol))
    ).scalar_one_or_none()
    if instrument_id is None:
        raise LookupError(f"no instrument with symbol {symbol!r}")

    history = await load_history(session, on)
    history.bars.filter(pl.col("instrument_id") == instrument_id)
    result = PolarsFactorEngine().run(history, on)
    row = result.frame.filter(pl.col("instrument_id") == instrument_id)

    explanation = Explanation(symbol, on, factor_key, window_lengths=result.window_lengths)
    if row.height == 0:
        explanation.notes.append(
            f"{symbol} has no bar on {on.isoformat()}, so no factor row exists for that date"
        )
        return explanation

    values = row.to_dicts()[0]
    factor = FACTORS.get(factor_key)
    if factor_key != "all" and factor is None:
        raise KeyError(
            f"unknown factor {factor_key!r}; not in the registry. "
            f"Try one of: {', '.join(sorted(FACTORS)[:8])}, ..."
        )

    if factor_key == "all":
        explanation.value = None
        explanation.inputs = {k: v for k, v in values.items() if not k.startswith("_")}
    else:
        assert factor is not None
        explanation.value = _evaluate(factor, values)
        explanation.inputs = _inputs_for(factor, values)
        explanation.notes.extend(_notes_for(factor))

    preview = (
        history.bars.filter(pl.col("instrument_id") == instrument_id)
        .sort("date")
        .tail(SERIES_PREVIEW)
        .select(["date", "close", "close_raw", "high", "low", "volume_raw", "turnover"])
    )
    explanation.series_preview = preview.to_dicts()
    return explanation


def _evaluate(factor: Factor, values: dict[str, object]) -> object:
    """The factor's value: a stored column, or its blend recomputed from the stored components."""
    if factor.is_stored:
        return values.get(factor.key)
    components = [values.get(name) for name in factor.components if name != "beta_12m"]
    if any(component is None for component in components):
        # docs/05 §4: "If **any** component is NULL the blend is NULL".
        return None
    numbers = [float(str(component)) for component in components]
    blended = sum(numbers) / len(numbers)
    if "beta_12m" in factor.components:
        beta = values.get("beta_12m")
        if beta is None or float(str(beta)) <= 0:
            # docs/05 §7: "beta <= 0 -> NULL".
            return None
        return blended / float(str(beta))
    return blended


def _inputs_for(factor: Factor, values: dict[str, object]) -> dict[str, object]:
    if factor.is_stored:
        return _stored_inputs(factor, values)
    return {name: values.get(name) for name in factor.components}


def _stored_inputs(factor: Factor, values: dict[str, object]) -> dict[str, object]:
    """The working behind a stored column, where docs/05 defines it as a ratio."""
    key = factor.key
    if key.startswith("sharpe_"):
        window = key.removeprefix("sharpe_")
        # docs/05 §3: sharpe_N = ret_N_pct / (vol_N_fraction x 100)
        return {
            f"ret_{window}": values.get(f"ret_{window}"),
            f"vol_{window}": values.get(f"vol_{window}"),
            "formula": "ret / (vol * 100)",
        }
    if key.startswith("away_high_"):
        high = "high_1y" if key.endswith("1y") else "high_ath"
        return {
            "close": values.get("close"),
            high: values.get(high),
            "formula": "(close / high - 1) * 100",
        }
    if key.startswith("ret_") and "minus" not in key:
        return {"close": values.get("close"), "formula": "(P_t / P_{t-N} - 1) * 100"}
    return {key: values.get(key)}


def _notes_for(factor: Factor) -> list[str]:
    notes = [f"family: {factor.family.value}", f"null policy: {factor.null_policy.value}"]
    if not factor.is_stored:
        notes.append(f"SQL: {factor.sql_expr}")
    if factor.key.startswith("ret_12m_minus"):
        notes.append(
            "INFERRED (docs/05 §8): the reference product's published values do not reconcile "
            "with this definition; see decile_core.momentum for the alternative reading."
        )
    if factor.key.startswith("circuits_"):
        notes.append("INFERRED (docs/05 §12): see decile_core.circuits for the detection rule.")
    return notes


def render_text(explanation: Explanation) -> str:
    lines = [
        f"{explanation.symbol} — {explanation.factor} on {explanation.date.isoformat()}",
        "=" * 72,
        f"value: {explanation.value}",
        "",
        "window lengths (trading days, recovered from the calendar):",
        "  " + ", ".join(f"{k}m={v}" for k, v in sorted(explanation.window_lengths.items())),
        "",
        "inputs:",
    ]
    lines += [f"  {name:<24} {value}" for name, value in explanation.inputs.items()]
    if explanation.series_preview:
        lines += ["", f"last {len(explanation.series_preview)} bars:"]
        lines += [
            "  " + "  ".join(f"{k}={v}" for k, v in row.items())
            for row in explanation.series_preview
        ]
    if explanation.notes:
        lines += ["", "notes:"]
        lines += [f"  - {note}" for note in explanation.notes]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m decile_worker.factors_cli", description=__doc__
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    explain_cmd = subcommands.add_parser("explain", help="show the working behind one factor")
    explain_cmd.add_argument("--symbol", required=True)
    explain_cmd.add_argument("--date", required=True, help="ISO trade date")
    explain_cmd.add_argument("--factor", required=True, help="a registry key, or 'all'")
    explain_cmd.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    on = dt.date.fromisoformat(args.date)

    try:
        explanation = run_in_session(lambda session: explain(session, args.symbol, on, args.factor))
    except (LookupError, KeyError) as exc:
        print(str(exc).strip("'"), file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(explanation.to_payload(), indent=2, sort_keys=True))
    else:
        print(render_text(explanation))
    return 0


#: Re-exported so the registry's SQL is reachable from the CLI's notes without a second import.
__all__ = ["Explanation", "blend_sql", "explain", "main", "render_text"]


if __name__ == "__main__":
    sys.exit(main())
