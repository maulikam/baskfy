"""The GitHub Actions workflows are loadable, and still run Prompt 19's gates.

WHY THIS FILE EXISTS
--------------------
It was written because `ci.yml` **was not valid YAML** and had not been since Prompt 16. The step
name

    - name: Client-JS budget (docs/11: screens route < 250 KB gzip)

is an unquoted scalar containing `: `, which YAML reads as a nested mapping key. GitHub Actions
refuses to run a workflow file it cannot parse, so every job in it — lint, tests, budgets,
the client-staleness gate — would have been skipped, and nothing in the repository would have
said so. Prompt 19's second acceptance criterion is "CI is green"; a file that never parsed
cannot be green or red, and that is worse.

Nothing here needs the network or a runner. It asserts the two things a broken workflow silently
loses: that the file parses at all, and that the steps a numbered prompt required are still in it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
WORKFLOWS = sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml"))


def _load(path: Path) -> dict[str, object]:
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    # YAML 1.1 reads a bare `on:` key as the boolean `True`. Normalise it back to the string the
    # file actually contains, so callers can ask for it by name.
    return {("on" if key is True else str(key)): value for key, value in parsed.items()}


def _jobs(workflow: dict[str, object]) -> dict[str, dict[str, object]]:
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict), "a workflow with no `jobs` mapping"
    return {str(name): body for name, body in jobs.items() if isinstance(body, dict)}


def _steps(workflow: dict[str, object]) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for job in _jobs(workflow).values():
        steps = job.get("steps")
        if isinstance(steps, list):
            out += [step for step in steps if isinstance(step, dict)]
    return out


def test_there_is_at_least_one_workflow() -> None:
    assert WORKFLOWS, f"no workflow files under {WORKFLOW_DIR}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_the_workflow_parses(path: Path) -> None:
    """The check that was missing. An unquoted `: ` in a step name is enough to break it."""
    try:
        workflow = _load(path)
    except yaml.YAMLError as exc:  # pragma: no cover - the message is the point
        pytest.fail(f"{path.name} is not loadable YAML: {exc}")
    assert "on" in workflow, f"{path.name} has no trigger"
    assert _jobs(workflow), f"{path.name} defines no jobs"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_job_has_steps(path: Path) -> None:
    for name, job in _jobs(_load(path)).items():
        assert job.get("steps"), f"{path.name}: job '{name}' has no steps"


class TestPromptNineteensGatesAreStillWired:
    """A gate that CI stopped running is a gate that does not exist."""

    @staticmethod
    @pytest.fixture(scope="class")
    def ci_commands() -> str:
        return "\n".join(
            str(step.get("run", "")) for step in _steps(_load(WORKFLOW_DIR / "ci.yml"))
        )

    def test_the_coverage_gate_runs(self, ci_commands: str) -> None:
        assert "tools.coverage_gate" in ci_commands
        assert "--cov-report=json" in ci_commands

    def test_the_reconciliation_report_runs(self, ci_commands: str) -> None:
        assert "baskfy_worker.reconcile_cli" in ci_commands

    def test_the_earlier_prompts_gates_are_still_there(self, ci_commands: str) -> None:
        """Prompts 7, 16 and 17 each added one. This is the one place they can all be checked."""
        for expected in (
            "baskfy_api.openapi",  # Prompt 7: the client-staleness gate
            "benchmarks.report",  # Prompt 16: the docs/11 budgets
            "test_query_plans.py",  # Prompt 16: the plan baseline
            "promtool",  # Prompt 17: the alert rules
            "bundle-budget",  # Prompt 16: the client-JS budget
        ):
            assert expected in ci_commands, f"CI no longer runs {expected}"
