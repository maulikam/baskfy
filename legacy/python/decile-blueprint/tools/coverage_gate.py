"""Per-package coverage thresholds (Prompt 19 §1).

    "Coverage: >= 90% on packages/core, >= 80% on services/api. Fail CI below those thresholds."

Why this exists rather than ``--cov-fail-under``
------------------------------------------------
``pytest-cov``'s ``--cov-fail-under`` is a single number over everything measured. Prompt 19 asks
for two different numbers over two different trees, and a combined figure hides exactly the case
that matters: ``baskfy_core`` sliding from 92% to 84% while ``baskfy_worker`` climbs, leaving the
total flat. So the thresholds are per package, and the packages Prompt 19 does *not* gate are
still reported — an ungated number that nobody can see is an ungated number that only falls.

Why the gate needs a database
-----------------------------
``services/api`` is 5,300 statements of endpoints, and most of them are exercised by tests marked
``db``. Without ``BASKFY_TEST_DATABASE_URL`` those skip, and ``baskfy_api`` measures far below
80% — not because the coverage is worse but because the suite did not run. The gate therefore
refuses to pass on a run where the database tests skipped, rather than reporting a number that
means something different from the one CI reports. ``make coverage`` and the CI job both provide
one.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

#: Prompt 19 §1's two thresholds, as percentages of statements-plus-branches.
THRESHOLDS: Final[dict[str, float]] = {
    "baskfy_core": 90.0,
    "baskfy_api": 80.0,
}

#: Measured and printed, but not gated: Prompt 19 names only the two above. Listing them here
#: rather than dropping them keeps the report honest about the whole codebase.
UNGATED: Final[tuple[str, ...]] = ("baskfy_providers", "baskfy_worker")

DEFAULT_REPORT: Final = Path("coverage.json")

#: The suite skips every ``db``-marked test without this, which silently removes most of
#: ``baskfy_api`` from the measurement. See the module docstring.
DATABASE_ENV_VAR: Final = "BASKFY_TEST_DATABASE_URL"


@dataclass(frozen=True, slots=True)
class PackageCoverage:
    """One package's branch-inclusive coverage, in the same arithmetic ``coverage`` uses."""

    package: str
    statements: int
    missing: int
    branches: int
    partial_branches: int

    @property
    def total(self) -> int:
        return self.statements + self.branches

    @property
    def covered(self) -> int:
        return (self.statements - self.missing) + (self.branches - self.partial_branches)

    @property
    def percent(self) -> float:
        return 100.0 * self.covered / self.total if self.total else 100.0


def package_of(path: str) -> str | None:
    """Which distribution a measured file belongs to, from its path."""
    for package in (*THRESHOLDS, *UNGATED):
        if f"/{package}/" in path.replace("\\", "/") or path.startswith(f"{package}/"):
            return package
    return None


def summarise(report: dict[str, object]) -> dict[str, PackageCoverage]:
    """Fold a ``coverage json`` report into one row per package."""
    files = report.get("files")
    if not isinstance(files, dict):
        raise ValueError("coverage report has no 'files' section; was --cov-report=json used?")

    totals: dict[str, list[int]] = {name: [0, 0, 0, 0] for name in (*THRESHOLDS, *UNGATED)}
    for path, entry in files.items():
        package = package_of(path)
        if package is None:
            continue
        summary = entry["summary"]
        bucket = totals[package]
        bucket[0] += int(summary["num_statements"])
        bucket[1] += int(summary["missing_lines"])
        bucket[2] += int(summary["num_branches"])
        bucket[3] += int(summary["num_partial_branches"])

    return {
        name: PackageCoverage(name, *values)
        for name, values in totals.items()
        if values[0] or values[2]
    }


def failures(
    summary: dict[str, PackageCoverage], thresholds: dict[str, float] = THRESHOLDS
) -> list[str]:
    """Every gated package that is below its threshold, or that was not measured at all."""
    out: list[str] = []
    for package, threshold in sorted(thresholds.items()):
        measured = summary.get(package)
        if measured is None:
            out.append(f"{package}: not present in the coverage report (did its tests run?)")
            continue
        if measured.percent < threshold:
            out.append(
                f"{package}: {measured.percent:.2f}% is below the {threshold:.0f}% "
                f"Prompt 19 requires ({measured.covered}/{measured.total} "
                "statements+branches)"
            )
    return out


def render(summary: dict[str, PackageCoverage]) -> str:
    lines = [f"{'package':<20} {'covered':>9} {'total':>9} {'cover':>8}  gate"]
    for package in sorted(summary):
        row = summary[package]
        threshold = THRESHOLDS.get(package)
        gate = f">= {threshold:.0f}%" if threshold is not None else "not gated"
        lines.append(
            f"{row.package:<20} {row.covered:>9} {row.total:>9} {row.percent:>7.2f}%  {gate}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--allow-no-database",
        action="store_true",
        help=(
            "report the numbers even though the db-marked tests skipped. Never use this to make "
            "a build green: the resulting baskfy_api figure is not comparable with CI's."
        ),
    )
    args = parser.parse_args(argv)

    if not os.environ.get(DATABASE_ENV_VAR) and not args.allow_no_database:
        print(
            f"{DATABASE_ENV_VAR} is not set, so every db-marked test skipped and this "
            "measurement is not the one Prompt 19 gates. Start the stack (`make up`) or pass "
            "--allow-no-database to see the numbers anyway.",
            file=sys.stderr,
        )
        return 2

    if not args.report.exists():
        print(f"no coverage report at {args.report}", file=sys.stderr)
        return 2

    report = json.loads(args.report.read_text())
    summary = summarise(report)
    print(render(summary))

    problems = failures(summary)
    if problems:
        print("\nCOVERAGE GATE FAILED (Prompt 19 §1):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("\ncoverage gate passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
