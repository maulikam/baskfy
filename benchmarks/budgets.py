"""docs/11 §"Performance budgets", as data — and the measurements taken against it.

Prompt 16's first acceptance criterion:

    "Every budget in docs/11 is met by an automated benchmark, and the numbers are written into
     docs/11 as an 'as measured' column."

The budget table below is transcribed from docs/11 verbatim; :data:`BUDGETS` is the single place
the numbers live, so a benchmark cannot quietly assert a looser figure than the document states.
Each measurement is written to :data:`RESULTS_DIR` as one JSON file per budget key, and
``benchmarks.report`` renders them into ``benchmarks/AS-MEASURED.md``.

Why the results land in a file rather than in an assertion message
------------------------------------------------------------------
Two reasons. A budget is met or missed by a *number*, and the number is worth keeping even when
the assertion passes — "150 ms budget, 6 ms measured" and "150 ms budget, 149 ms measured" are
very different states of health and only one of them is one bad night from a page. And the
benchmarks run across several pytest processes and two languages (the bundle budget is measured
by ``apps/web/scripts/bundle-budget.mjs``), so a shared directory is the only place they can all
report to.

Why the table is not written into docs/11 itself
------------------------------------------------
The overnight working agreement this module was built under forbids editing anything under
``docs/`` except appending to ``docs/DECISIONS.md``. The rendered table therefore lives at
``benchmarks/AS-MEASURED.md``, ready to be pasted in as the "As measured" column by a human who
is allowed to touch the spec. Recorded in ``docs/DECISIONS.md`` §16.1.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
RESULTS_DIR: Final = REPO_ROOT / "benchmarks" / "results"
REPORT_PATH: Final = REPO_ROOT / "benchmarks" / "AS-MEASURED.md"

Unit = Literal["ms", "s", "min", "KB"]


@dataclass(frozen=True, slots=True)
class Budget:
    """One row of docs/11 §"Performance budgets"."""

    key: str
    #: The "Surface" column, verbatim.
    surface: str
    #: The "Target" column, verbatim.
    target: str
    #: The target as a number, in :attr:`unit`. ``None`` where docs/11 states no single number.
    limit: float | None
    unit: Unit
    #: Which harness produces this measurement, so an unmeasured row says where to look.
    measured_by: str
    #: Which CI job is allowed to fail on this row.
    #:
    #: ``"python"`` — ``pytest -m benchmark`` produces it, so the Python job fails if it is
    #: missed *or* unmeasured (a benchmark that quietly stopped running must not read as a pass).
    #: ``"web"`` — measured by the web job's own tooling, which does its own failing.
    #: ``"none"`` — reported but never gated, because nothing can measure it yet. Every one of
    #: these is called out in ``.overnight/module-16.md``; they are gaps, not exemptions.
    gate: Literal["python", "web", "none"] = "python"


#: docs/11 §"Performance budgets", transcribed. Order preserved.
BUDGETS: Final[tuple[Budget, ...]] = (
    Budget(
        "screen_run_warm",
        "Screen run, warm cache",
        "p95 < 150 ms",
        150.0,
        "ms",
        "services/api/tests/test_api_benchmark.py",
    ),
    Budget(
        "screen_run_cold",
        "Screen run, cold",
        "p95 < 800 ms",
        800.0,
        "ms",
        "services/api/tests/test_api_benchmark.py",
    ),
    Budget(
        "factsheet_ttfb",
        "Instrument factsheet (RSC) — TTFB",
        "TTFB < 300 ms",
        300.0,
        "ms",
        "services/api/tests/test_benchmarks.py",
    ),
    Budget(
        "factsheet_lcp",
        "Instrument factsheet (RSC) — LCP",
        "LCP < 1.8 s",
        1.8,
        "s",
        "apps/web/e2e/performance.spec.ts",
        "web",
    ),
    Budget(
        "dashboard",
        "Dashboard (145 indices)",
        "< 500 ms",
        500.0,
        "ms",
        "services/api/tests/test_benchmarks.py",
    ),
    Budget(
        "backtest_15y",
        "Backtest (15y, monthly, 20 names)",
        "< 10 s",
        10.0,
        "s",
        "packages/core/tests/test_backtest.py",
    ),
    Budget(
        "nightly_pipeline",
        "Nightly pipeline end-to-end",
        "< 45 min",
        45.0,
        "min",
        "not measured — needs a real backfill (see benchmarks/README.md)",
        "none",
    ),
    Budget(
        "compute_factors",
        "…of which: compute_factors (2,300 instruments)",
        "no separate budget in docs/11",
        None,
        "s",
        "packages/core/tests/test_factor_benchmark.py",
    ),
    Budget(
        "csv_export",
        "CSV export (4,000 rows)",
        "< 2 s",
        2.0,
        "s",
        "services/api/tests/test_benchmarks.py",
    ),
    Budget(
        "load_50_concurrent",
        "50 concurrent screen runs (Prompt 16 acceptance criterion, not docs/11)",
        "p95 < 400 ms, no errors",
        400.0,
        "ms",
        "services/api/tests/test_load.py",
    ),
    Budget(
        "screens_bundle",
        "Client JS on the screens route",
        "< 250 KB gzip",
        250.0,
        "KB",
        "apps/web/scripts/bundle-budget.mjs",
        "web",
    ),
)

BUDGET_BY_KEY: Final[dict[str, Budget]] = {budget.key: budget for budget in BUDGETS}


@dataclass(frozen=True, slots=True)
class Measurement:
    key: str
    value: float
    unit: Unit
    #: Free text naming the shape of the measurement — "p95 of 40 samples", "median of 5 runs".
    method: str
    #: What the number was measured against. The honesty column: "the seeded dataset" and "a
    #: synthetic 2,300-instrument universe" are not the same claim.
    dataset: str

    @property
    def budget(self) -> Budget:
        return BUDGET_BY_KEY[self.key]

    @property
    def within_budget(self) -> bool | None:
        limit = self.budget.limit
        return None if limit is None else self.value < limit


def record(key: str, value: float, *, unit: Unit, method: str, dataset: str) -> Measurement:
    """Write one measurement, for ``benchmarks.report`` to pick up.

    Called from inside a benchmark test after its assertion, so a *failing* budget records
    nothing — the report then shows it as unmeasured, which is the honest state: we do not have a
    number we are prepared to publish for it.
    """
    if key not in BUDGET_BY_KEY:
        raise KeyError(f"{key!r} is not a docs/11 budget; add it to BUDGETS first")
    measurement = Measurement(key=key, value=value, unit=unit, method=method, dataset=dataset)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / f"{key}.json").write_text(
        json.dumps(
            {
                "key": key,
                "value": value,
                "unit": unit,
                "method": method,
                "dataset": dataset,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return measurement


def load_all(directory: Path = RESULTS_DIR) -> dict[str, Measurement]:
    """Every measurement recorded so far, keyed by budget."""
    if not directory.exists():
        return {}
    found: dict[str, Measurement] = {}
    for path in sorted(directory.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        key = str(document["key"])
        if key not in BUDGET_BY_KEY:
            continue
        found[key] = Measurement(
            key=key,
            value=float(document["value"]),
            unit=_as_unit(str(document["unit"])),
            method=str(document["method"]),
            dataset=str(document["dataset"]),
        )
    return found


#: Every unit the table uses. A tuple as well as a ``Literal`` because a value read back out of
#: JSON is a plain ``str`` and has to be narrowed before it is one of them again.
UNITS: Final[tuple[Unit, ...]] = ("ms", "s", "min", "KB")


def _as_unit(raw: str) -> Unit:
    for unit in UNITS:
        if raw == unit:
            return unit
    raise ValueError(f"{raw!r} is not a unit this table uses; expected one of {UNITS}")
