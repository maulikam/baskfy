"""Mutation testing for the two modules the whole product rests on (Prompt 19 §6).

    "Mutation testing (mutmut or cosmic-ray) on packages/core/factors.py and screener.py; fix or
     justify every surviving mutant."

WHY THIS IS NOT MUTMUT
----------------------
It was tried first, and the attempt is worth recording because the failure is structural rather
than a configuration mistake. ``mutmut`` 3.7 works by copying ``source_paths`` into a ``mutants/``
directory and running pytest from there. This repository is a **uv workspace of four
editable, src-layout packages**: ``baskfy_core`` resolves through
``.venv/.../_editable_impl_baskfy_core.pth``, which is an absolute path to the *real*
``packages/core/src``. Under ``mutmut run`` the tests therefore import the unmutated module,
mutmut's stats phase attributes no test to any mutant, and it stops with "we could not find any
test case for any mutant". Prepending the mutant tree to ``PYTHONPATH`` does not help: mutmut
re-execs pytest itself and its own coverage attribution runs against the copy it made.
``cosmic-ray`` has the same shape of problem — it mutates in place in a session database and
needs a single importable package root.

So the harness below does what mutmut's model does, in the way this layout supports: it copies
``baskfy_core`` to a temporary directory **once**, mutates one file in the copy per mutant, and
runs pytest with that directory first on ``PYTHONPATH`` so the copy shadows the editable install.
The real source tree is never written to, which matters for a tool that is normally run
unattended.

``docs/DECISIONS.md`` §19.4.

WHAT COUNTS AS A KILL
---------------------
A mutant is **killed** if the scoped test selection fails or errors, and **survives** if it
passes. A mutant whose module cannot even be imported is killed too (a syntax-or-type error is a
failure the suite reports). Test files are ordered cheapest-first and pytest runs with ``-x``, so
a killed mutant costs one fast test file rather than the whole selection.

MUTATION OPERATORS
------------------
Six, chosen because each corresponds to a bug class docs/05 or docs/06 would actually suffer:

* comparison swap (``>`` <-> ``>=``, ``<`` <-> ``<=``, ``==`` <-> ``!=``) — off-by-one in a
  window boundary or a threshold;
* arithmetic swap (``+`` <-> ``-``, ``*`` <-> ``/``) — a units or sign error;
* boolean operator swap (``and`` <-> ``or``) — a filter that keeps the wrong rows;
* numeric constant shift (``n`` -> ``n + 1``, and ``0`` <-> ``1``) — a wrong window length,
  annualisation constant or decile count;
* boolean constant flip (``True`` <-> ``False``) — an inverted default;
* ``not`` removal — an inverted guard.

Docstrings, type annotations and ``__all__`` are not mutated: a change there cannot alter a
number, so a surviving mutant would be noise.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from queue import Queue
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
PACKAGE_ROOT: Final = REPO_ROOT / "packages" / "core" / "src"
PACKAGE: Final = "baskfy_core"

#: Prompt 19 §6 names exactly these two.
FACTOR_TARGETS: Final[tuple[str, ...]] = ("factors.py", "screener.py")

#: SW1 (docs/swing/06-module-plan.md): ``baskfy_core.swing`` joins the harness "at the same
#: threshold as ``factors``". The swing package decides what is bought, at what size and with
#: what stop, so an off-by-one in a window boundary or an inverted guard there is the same class
#: of bug the factor engine is mutated for — and its detectors are almost entirely comparisons
#: against thresholds, which is exactly what these six operators perturb.
#:
#: ``config.py`` is deliberately absent. It is a table of defaults with no logic; shifting
#: ``adr_min_pct`` from 3.5 to 4.5 changes what a *calibration* means, not whether the code
#: implements the calibration correctly, and every such mutant would "survive" against tests
#: that (correctly) build their own config. ``__init__.py`` is re-exports only.
SWING_TARGETS: Final[tuple[str, ...]] = (
    "swing/indicators.py",
    "swing/setups.py",
    "swing/sizing.py",
    "swing/stops.py",
    "swing/market.py",
    "swing/opening_range.py",
    "swing/plan.py",
    "swing/journal.py",
    # SW9: the EOD backtest. Its own logic is the session order, the entry rule, the fill prices
    # and the funnel — comparisons and arithmetic again — and its tests plant a trade whose every
    # rupee is worked by hand, so a flipped `>=` in the entry rule or a `+` for a `-` in a cost
    # shows up as a different R.
    "swing/backtest.py",
)

TARGETS: Final[tuple[str, ...]] = FACTOR_TARGETS + SWING_TARGETS

#: Cheapest first: a mutant that breaks the screener usually dies in ``test_screener.py`` in a
#: couple of seconds, and never reaches the slower property and cross-validation suites.
TEST_SELECTION: Final[tuple[str, ...]] = (
    "packages/core/tests/test_factor_edge_guards.py",
    "packages/core/tests/test_screener.py",
    "packages/core/tests/test_factors_golden.py",
    "packages/core/tests/test_factor_properties.py",
    "packages/core/tests/test_factor_crossvalidation.py",
    "packages/core/tests/test_reference_export.py",
    "packages/core/tests/test_reference_parity.py",
)

#: The swing suite, in the order a mutant is scored against it — see :func:`selection_for`.
#:
#: ``test_swing_purity.py`` and ``test_swing_docs_parity.py`` are **deliberately absent**. Neither
#: imports the modules: purity is a line scan over the source and the docs-parity check reads the
#: config dataclasses against the specification. Neither can fail because a comparison flipped, so
#: including them would add two pytest start-ups to every one of the several hundred mutants and
#: kill exactly none of them.
SWING_TEST_SELECTION: Final[tuple[str, ...]] = (
    "packages/core/tests/test_swing_contract_detectors.py",
    "packages/core/tests/test_swing_contract_book.py",
    "packages/core/tests/test_swing_contract_edges.py",
    "packages/core/tests/test_swing_setups.py",
    "packages/core/tests/test_swing_sizing.py",
    "packages/core/tests/test_swing_stops.py",
    "packages/core/tests/test_swing_market.py",
    "packages/core/tests/test_swing_opening_range.py",
    "packages/core/tests/test_swing_plan_and_journal.py",
    # SW12: the two files SW10 / SW10.5 added after this list was written. Without them every
    # mutant of `first_live_multiplier`, `sizing_at`, `marketable_limit` and the PENDING_RANGE
    # line "survived" — not because the rules were untested, but because the tests that assert
    # A7, A8 and A9 were never in the selection.
    "packages/core/tests/test_swing_pending_and_first_live.py",
    "packages/core/tests/test_swing_safety_properties.py",
    # Last, because it is the slowest: a mutant of any other swing module that survives the nine
    # files above still has to survive a planted trade run end to end. Its speed test skips
    # itself inside the harness's workspace (a mutant's speed is not evidence).
    "packages/core/tests/test_swing_backtest.py",
)

#: The one file most likely to kill a mutant of each module, tried first. ``pytest`` runs with
#: ``-x`` and the runner stops at the first failing file, so putting the module's own tests at the
#: head is the difference between one start-up per killed mutant and six.
SWING_PRIMARY_TEST: Final[dict[str, str]] = {
    "swing/indicators.py": "packages/core/tests/test_swing_contract_detectors.py",
    "swing/setups.py": "packages/core/tests/test_swing_contract_detectors.py",
    "swing/sizing.py": "packages/core/tests/test_swing_contract_book.py",
    "swing/stops.py": "packages/core/tests/test_swing_contract_book.py",
    "swing/market.py": "packages/core/tests/test_swing_contract_book.py",
    "swing/opening_range.py": "packages/core/tests/test_swing_contract_book.py",
    "swing/plan.py": "packages/core/tests/test_swing_contract_book.py",
    "swing/journal.py": "packages/core/tests/test_swing_contract_book.py",
    "swing/backtest.py": "packages/core/tests/test_swing_backtest.py",
}


def selection_for(target: str) -> tuple[str, ...]:
    """Which test files a mutant of ``target`` is scored against, in which order.

    A per-target selection, not one list for everything: running the factor suites against a
    swing mutant would score every one of them "survived" (they never import the package), and
    running the swing suites against a factor mutant would do the same in reverse. A mutation
    score is only evidence when the tests in the selection are the tests that *could* fail.
    """
    if not target.startswith("swing/"):
        return TEST_SELECTION
    primary = SWING_PRIMARY_TEST[target]
    return (primary, *(path for path in SWING_TEST_SELECTION if path != primary))


#: Where the mutated copies live. **Inside the repository, deliberately.**
#: ``baskfy_core.reference_export.default_fixture_path`` finds the committed CSV by walking
#: ``Path(__file__).parents`` for a ``tests/fixtures/`` directory. A copy under ``/tmp`` has no
#: such parent, so every test that reads the export raises ``FileNotFoundError`` — and every
#: mutant is scored "killed" for a reason that has nothing to do with the mutation. That is
#: exactly the false 100% the `control` pre-flight exists to catch, and it caught it. Putting the
#: workspaces under the repository root restores the parent chain. Git-ignored.
WORKSPACE_ROOT: Final = REPO_ROOT / ".mutants"

DEFAULT_REPORT: Final = REPO_ROOT / "reconciliation" / "MUTANTS.md"
DEFAULT_JSON: Final = REPO_ROOT / "reconciliation" / "mutants.json"


@dataclass(frozen=True, slots=True)
class Mutant:
    """One source-level change, addressable by ``file:line:operator:index``."""

    file: str
    line: int
    column: int
    operator: str
    before: str
    after: str
    source: str

    @property
    def identifier(self) -> str:
        return f"{self.file}:{self.line}:{self.column}:{self.operator}"

    @property
    def label(self) -> str:
        return f"{self.identifier}  {self.before} -> {self.after}"


@dataclass(frozen=True, slots=True)
class Outcome:
    identifier: str
    file: str
    line: int
    operator: str
    before: str
    after: str
    killed: bool
    killed_by: str | None


# ---------------------------------------------------------------------------
# Generating mutants
# ---------------------------------------------------------------------------

_COMPARISON_SWAPS: Final[dict[type[ast.cmpop], type[ast.cmpop]]] = {
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
}
_BINARY_SWAPS: Final[dict[type[ast.operator], type[ast.operator]]] = {
    ast.Add: ast.Sub,
    ast.Sub: ast.Add,
    ast.Mult: ast.Div,
    ast.Div: ast.Mult,
}


class _Mutator(ast.NodeTransformer):
    """Applies exactly one mutation, identified by its ordinal among all candidate sites."""

    def __init__(self, target: int, skip_lines: frozenset[int] = frozenset()) -> None:
        self.target = target
        self.skip_lines = skip_lines
        self.seen = -1
        self.applied: tuple[str, str, str, int, int] | None = None

    def _next(self) -> bool:
        self.seen += 1
        return self.seen == self.target

    # -- comparisons -------------------------------------------------------
    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        self.generic_visit(node)
        for index, op in enumerate(node.ops):
            replacement = _COMPARISON_SWAPS.get(type(op))
            if replacement is None:
                continue
            if self._next():
                before, after = type(op).__name__, replacement.__name__
                node.ops[index] = replacement()
                self.applied = ("comparison", before, after, node.lineno, node.col_offset)
        return node

    # -- arithmetic --------------------------------------------------------
    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        self.generic_visit(node)
        replacement = _BINARY_SWAPS.get(type(node.op))
        if replacement is not None and self._next():
            before, after = type(node.op).__name__, replacement.__name__
            node.op = replacement()
            self.applied = ("arithmetic", before, after, node.lineno, node.col_offset)
        return node

    # -- and / or ----------------------------------------------------------
    def visit_BoolOp(self, node: ast.BoolOp) -> ast.AST:
        self.generic_visit(node)
        if self._next():
            replacement = ast.Or() if isinstance(node.op, ast.And) else ast.And()
            before, after = type(node.op).__name__, type(replacement).__name__
            node.op = replacement
            self.applied = ("boolean_operator", before, after, node.lineno, node.col_offset)
        return node

    # -- not ---------------------------------------------------------------
    def visit_UnaryOp(self, node: ast.UnaryOp) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.op, ast.Not) and self._next():
            self.applied = ("not_removal", "not x", "x", node.lineno, node.col_offset)
            return node.operand
        return node

    # -- constants ---------------------------------------------------------
    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if node.lineno in self.skip_lines:
            return node
        if isinstance(node.value, bool):
            if self._next():
                self.applied = (
                    "boolean_constant",
                    repr(node.value),
                    repr(not node.value),
                    node.lineno,
                    node.col_offset,
                )
                return ast.copy_location(ast.Constant(value=not node.value), node)
            return node
        if isinstance(node.value, int | float) and not isinstance(node.value, bool):
            if self._next():
                shifted: int | float = 1 if node.value == 0 else node.value + 1
                self.applied = (
                    "numeric_constant",
                    repr(node.value),
                    repr(shifted),
                    node.lineno,
                    node.col_offset,
                )
                return ast.copy_location(ast.Constant(value=shifted), node)
            return node
        return node


def _docstring_lines(tree: ast.Module) -> frozenset[int]:
    """The line of every docstring, so its characters are never a mutation site.

    Identified by line rather than by node identity because each mutant re-parses the *original*
    source — which is what keeps every reported line number a line number in the real file, not in
    an unparsed round trip.
    """
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            first = body[0].value
            lines.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return frozenset(lines)


def generate(path: Path) -> list[Mutant]:
    """Every mutant of one file, in a stable order, with the *original* line numbers."""
    original = path.read_text(encoding="utf-8")
    skip = _docstring_lines(ast.parse(original))

    # Count the candidate sites by running the mutator with an unreachable target.
    counter = _Mutator(-1, skip)
    counter.visit(ast.parse(original))
    total = counter.seen + 1

    mutants: list[Mutant] = []
    for index in range(total):
        mutator = _Mutator(index, skip)
        mutated = mutator.visit(ast.parse(original))
        if mutator.applied is None:
            continue
        operator, before, after, line, column = mutator.applied
        source = ast.unparse(ast.fix_missing_locations(mutated))
        mutants.append(Mutant(_relative(path), line, column, operator, before, after, source))
    return mutants


def _relative(path: Path) -> str:
    """The target's path *inside the package*, e.g. ``swing/setups.py``.

    It used to be ``path.name``. That was indistinguishable from the relative path while every
    target sat at the top of ``baskfy_core``; the moment SW1 added ``swing/setups.py`` it would
    have written the mutated file to ``baskfy_core/setups.py`` — a module nothing imports — and
    every swing mutant would have been scored "survived" against an unmutated package.
    """
    try:
        return path.resolve().relative_to(PACKAGE_ROOT / PACKAGE).as_posix()
    except ValueError:
        return path.name


# ---------------------------------------------------------------------------
# Running them
# ---------------------------------------------------------------------------


def _run_selection(package_dir: Path, selection: tuple[str, ...]) -> str | None:
    """Run the scoped tests against ``package_dir``; return the file that killed the mutant."""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(package_dir.parent)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    # Hypothesis writes every falsifying example it finds into `.hypothesis/` and replays it on
    # the next run. Without this, a mutant's falsifying example is replayed against the *real*
    # suite afterwards — a mutation run would leave the developer's next `pytest` red for reasons
    # that have nothing to do with their change. Each mutation run gets its own throwaway store.
    environment["HYPOTHESIS_STORAGE_DIRECTORY"] = str(package_dir.parent / ".hypothesis")
    for test_file in selection:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                test_file,
                "-x",
                "-q",
                "--no-header",
                "-p",
                "no:cacheprovider",
                "-p",
                "no:randomly",
            ],
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,  # a non-zero exit is the signal, not an error
        )
        if completed.returncode != 0:
            return test_file
    return None


def _evaluate(mutant: Mutant, workspace: Path, selection: tuple[str, ...]) -> Outcome:
    package_dir = workspace / PACKAGE
    (package_dir / mutant.file).write_text(mutant.source, encoding="utf-8")
    try:
        killed_by = _run_selection(package_dir, selection)
    except subprocess.TimeoutExpired:
        killed_by = "timeout"
    return Outcome(
        mutant.identifier,
        mutant.file,
        mutant.line,
        mutant.operator,
        mutant.before,
        mutant.after,
        killed_by is not None,
        killed_by,
    )


def control(target: str, workspace: Path, selection: tuple[str, ...]) -> str | None:
    """The pre-flight that makes a 100% score believable.

    Every mutant is written as ``ast.unparse`` output, which throws away comments, ``# noqa``
    markers and the original formatting. If that round trip alone broke the module, *every* mutant
    would be "killed" and the score would be 100% while proving nothing. So before any mutant runs,
    the unparsed-but-unmutated module is installed and the same selection is run: it must pass.
    Returns the test file that failed, or None.
    """
    original = (PACKAGE_ROOT / PACKAGE / target).read_text(encoding="utf-8")
    unparsed = ast.unparse(ast.parse(original))
    (workspace / PACKAGE / target).write_text(unparsed, encoding="utf-8")
    try:
        return _run_selection(workspace / PACKAGE, selection)
    finally:
        shutil.copy2(PACKAGE_ROOT / PACKAGE / target, workspace / PACKAGE / target)


def run(
    mutants: list[Mutant], selection: tuple[str, ...] | None = None, workers: int = 4
) -> list[Outcome]:
    """Evaluate every mutant. Each worker gets its own pristine copy of the package."""
    outcomes: list[Outcome] = []
    if WORKSPACE_ROOT.exists():
        shutil.rmtree(WORKSPACE_ROOT)
    WORKSPACE_ROOT.mkdir(parents=True)
    try:
        workspaces = []
        for index in range(workers):
            workspace = WORKSPACE_ROOT / f"w{index}"
            workspace.mkdir()
            shutil.copytree(PACKAGE_ROOT / PACKAGE, workspace / PACKAGE)
            workspaces.append(workspace)

        # Pre-flight: see `control`. Without it a 100% score is not evidence of anything.
        for target in sorted({mutant.file for mutant in mutants}):
            broken_by = control(target, workspaces[0], selection or selection_for(target))
            if broken_by is not None:
                raise RuntimeError(
                    f"the unparsed-but-unmutated {target} fails {broken_by}. Every mutant would "
                    "be scored as killed and the run would be meaningless."
                )
            print(f"  control ok: unmutated {target} still passes the selection", flush=True)

        # A LEASE, NOT `index % workers`. Threads do not process indices in lockstep — if one
        # mutant dies in two seconds and another takes ninety, indices 0 and `workers` can be in
        # flight at the same moment and would share a workspace, each overwriting the other's
        # mutated file. That race produced a run where `TRADING_DAYS_PER_YEAR: 252 -> 253`
        # "survived". A worker holds a workspace for the whole evaluation and nothing else can
        # touch it.
        available: Queue[Path] = Queue()
        for workspace in workspaces:
            available.put(workspace)

        def task(mutant: Mutant) -> Outcome:
            workspace = available.get()
            try:
                outcome = _evaluate(mutant, workspace, selection or selection_for(mutant.file))
            finally:
                # Restore the pristine file before the workspace goes back in the pool.
                shutil.copy2(
                    PACKAGE_ROOT / PACKAGE / mutant.file, workspace / PACKAGE / mutant.file
                )
                available.put(workspace)
            print(
                f"  {'killed ' if outcome.killed else 'SURVIVED'} {mutant.label}",
                flush=True,
            )
            return outcome

        with ThreadPoolExecutor(max_workers=workers) as pool:
            outcomes = list(pool.map(task, mutants))
    finally:
        shutil.rmtree(WORKSPACE_ROOT, ignore_errors=True)
    return outcomes


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def render(outcomes: list[Outcome], justifications: dict[str, str]) -> str:
    killed = [o for o in outcomes if o.killed]
    survivors = [o for o in outcomes if not o.killed]
    score = 100.0 * len(killed) / len(outcomes) if outcomes else 100.0

    modules = sorted(
        {f"`baskfy_core.{o.file.removesuffix('.py').replace('/', '.')}`" for o in outcomes}
    )
    lines = [
        "# Mutation testing — " + ", ".join(modules),
        "",
        'Generated by `make mutants`. Prompt 19 §6: "fix or justify every surviving mutant."',
        "SW1 added the `baskfy_core.swing` modules at the same threshold as `factors`.",
        "",
        f"- mutants generated: **{len(outcomes)}**",
        f"- killed: **{len(killed)}**",
        f"- survived: **{len(survivors)}**",
        f"- mutation score: **{score:.1f}%**",
        "",
        "## By file",
        "",
        "| file | mutants | killed | score |",
        "|---|---|---|---|",
    ]
    by_file: dict[str, list[Outcome]] = {}
    for outcome in outcomes:
        by_file.setdefault(outcome.file, []).append(outcome)
    for name in sorted(by_file):
        group = by_file[name]
        dead = sum(1 for o in group if o.killed)
        lines.append(f"| `{name}` | {len(group)} | {dead} | {100.0 * dead / len(group):.1f}% |")
    lines += [
        "",
        "## Survivors",
        "",
    ]
    if not survivors:
        lines += ["None.", ""]
    else:
        lines += ["| mutant | operator | change | justification |", "|---|---|---|---|"]
        for survivor in sorted(survivors, key=lambda o: (o.file, o.line)):
            reason = justifications.get(survivor.identifier, "**UNJUSTIFIED — needs a decision**")
            lines.append(
                f"| `{survivor.file}:{survivor.line}` | {survivor.operator} | "
                f"`{survivor.before}` -> `{survivor.after}` | {reason} |"
            )
        lines.append("")

    by_operator: dict[str, list[Outcome]] = {}
    for outcome in outcomes:
        by_operator.setdefault(outcome.operator, []).append(outcome)
    lines += ["## By operator", "", "| operator | mutants | killed | score |", "|---|---|---|---|"]
    for operator in sorted(by_operator):
        group = by_operator[operator]
        alive = sum(1 for o in group if o.killed)
        lines.append(f"| {operator} | {len(group)} | {alive} | {100.0 * alive / len(group):.1f}% |")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets", nargs="*", default=list(TARGETS))
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0, help="0 = every mutant")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    parser.add_argument(
        "--justifications",
        type=Path,
        default=REPO_ROOT / "reconciliation" / "mutant-justifications.json",
    )
    parser.add_argument("--list", action="store_true", help="generate mutants and stop")
    args = parser.parse_args(argv)

    mutants: list[Mutant] = []
    for target in args.targets:
        mutants += generate(PACKAGE_ROOT / PACKAGE / target)
    if args.limit:
        mutants = mutants[: args.limit]

    print(f"{len(mutants)} mutants across {', '.join(args.targets)}", flush=True)
    if args.list:
        for mutant in mutants:
            print(f"  {mutant.label}")
        return 0

    outcomes = run(mutants, workers=args.workers)
    justifications: dict[str, str] = {}
    if args.justifications.is_file():
        justifications = json.loads(args.justifications.read_text())

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(render(outcomes, justifications), encoding="utf-8")
    args.json.write_text(
        json.dumps([asdict(o) for o in outcomes], indent=2, sort_keys=True), encoding="utf-8"
    )

    survivors = [o for o in outcomes if not o.killed]
    unjustified = [o for o in survivors if o.identifier not in justifications]
    print(f"\n{len(outcomes) - len(survivors)}/{len(outcomes)} killed; {len(survivors)} survived")
    print(f"report: {args.report}")
    if unjustified:
        print(f"\n{len(unjustified)} survivor(s) with no written justification:", file=sys.stderr)
        for outcome in unjustified[:40]:
            print(f"  {outcome.identifier}  {outcome.before} -> {outcome.after}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
