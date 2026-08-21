"""The per-package coverage gate (Prompt 19 §1).

    "Coverage: >= 90% on packages/core, >= 80% on services/api. Fail CI below those thresholds."

The gate is what turns a number in a build log into a build failure, so it needs its own tests:
one that shows it passes when the thresholds are met, and — the ones that matter — several that
show it *fails* when they are not. A gate nobody has watched fail is not a gate.

These run on synthetic coverage reports rather than on a real one. A test that ran coverage over
the whole repository to assert the gate would take twenty minutes and would fail for reasons
having nothing to do with the gate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tools.coverage_gate import (
    THRESHOLDS,
    UNGATED,
    PackageCoverage,
    failures,
    main,
    package_of,
    render,
    summarise,
)

#: One measured file, in `coverage json`'s shape. Spelled out rather than typed as `Any`:
#: CLAUDE.md house rule 3 bans `Any`, and `object` here would need a cast at every use.
FileEntry = dict[str, dict[str, int]]
Files = dict[str, FileEntry]


def _file(statements: int, missing: int, branches: int = 0, partial: int = 0) -> FileEntry:
    return {
        "summary": {
            "num_statements": statements,
            "missing_lines": missing,
            "num_branches": branches,
            "num_partial_branches": partial,
        }
    }


def _files(core: tuple[int, int], api: tuple[int, int]) -> Files:
    return {
        "packages/core/src/decile_core/factors.py": _file(*core),
        "services/api/src/decile_api/app.py": _file(*api),
        "packages/providers/src/decile_providers/nse.py": _file(100, 5),
        "services/worker/src/decile_worker/orchestrator.py": _file(100, 40),
        # Something outside every measured package: it must not be attributed anywhere.
        "/usr/lib/python3.12/json/decoder.py": _file(500, 500),
    }


def _report(core: tuple[int, int], api: tuple[int, int]) -> dict[str, object]:
    return {"files": _files(core, api)}


class TestAttribution:
    def test_a_file_is_attributed_to_its_package(self) -> None:
        assert package_of("packages/core/src/decile_core/factors.py") == "decile_core"
        assert package_of("/abs/services/api/src/decile_api/app.py") == "decile_api"

    def test_a_file_outside_every_package_is_attributed_to_none(self) -> None:
        assert package_of("/usr/lib/python3.12/json/decoder.py") is None

    def test_the_stdlib_does_not_dilute_a_package(self) -> None:
        summary = summarise(_report((1000, 50), (1000, 100)))
        assert set(summary) == {*THRESHOLDS, *UNGATED}
        assert summary["decile_core"].total == 1000


class TestTheArithmetic:
    def test_branches_count_towards_the_percentage(self) -> None:
        """`[tool.coverage.run] branch = true`, so a gate on statements alone would be looser."""
        row = PackageCoverage(
            "decile_core", statements=100, missing=0, branches=100, partial_branches=50
        )
        assert row.total == 200
        assert row.percent == pytest.approx(75.0)

    def test_an_empty_package_is_not_a_division_by_zero(self) -> None:
        assert PackageCoverage("decile_core", 0, 0, 0, 0).percent == 100.0


class TestTheGate:
    def test_it_passes_when_both_thresholds_are_met(self) -> None:
        assert failures(summarise(_report((1000, 50), (1000, 150)))) == []

    def test_it_fails_when_core_is_below_ninety(self) -> None:
        problems = failures(summarise(_report((1000, 150), (1000, 100))))
        assert len(problems) == 1
        assert problems[0].startswith("decile_core: 85.00% is below the 90%")

    def test_it_fails_when_api_is_below_eighty(self) -> None:
        problems = failures(summarise(_report((1000, 50), (1000, 250))))
        assert len(problems) == 1
        assert problems[0].startswith("decile_api: 75.00% is below the 80%")

    def test_a_package_missing_from_the_report_is_a_failure_not_a_pass(self) -> None:
        """The dangerous case: its tests did not run, so it has no number to be below."""
        report: dict[str, object] = {
            "files": {"packages/core/src/decile_core/factors.py": _file(100, 0)}
        }
        problems = failures(summarise(report))
        assert problems == ["decile_api: not present in the coverage report (did its tests run?)"]

    def test_the_ungated_packages_are_reported_but_never_fail_the_build(self) -> None:
        files = _files((1000, 50), (1000, 100))
        files["services/worker/src/decile_worker/orchestrator.py"] = _file(1000, 900)
        summary = summarise({"files": files})
        assert summary["decile_worker"].percent < 80
        assert failures(summary) == []
        assert "not gated" in render(summary)

    def test_a_report_with_no_files_section_is_an_error(self) -> None:
        empty: dict[str, object] = {"totals": {}}
        with pytest.raises(ValueError, match="no 'files' section"):
            summarise(empty)


class TestTheCommand:
    def test_it_refuses_to_run_without_a_database(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Without one the db-marked tests skip and decile_api is not the number CI gates."""
        monkeypatch.delenv("DECILE_TEST_DATABASE_URL", raising=False)
        report = tmp_path / "coverage.json"
        report.write_text(json.dumps(_report((1000, 50), (1000, 100))))
        assert main(["--report", str(report)]) == 2
        assert "is not set" in capsys.readouterr().err

    def test_it_passes_with_a_database_and_a_healthy_report(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("DECILE_TEST_DATABASE_URL", "postgresql+asyncpg://x/y")
        report = tmp_path / "coverage.json"
        report.write_text(json.dumps(_report((1000, 50), (1000, 100))))
        assert main(["--report", str(report)]) == 0
        assert "coverage gate passed" in capsys.readouterr().out

    def test_it_fails_the_build_on_a_thin_report(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("DECILE_TEST_DATABASE_URL", "postgresql+asyncpg://x/y")
        report = tmp_path / "coverage.json"
        report.write_text(json.dumps(_report((1000, 400), (1000, 400))))
        assert main(["--report", str(report)]) == 1
        assert "COVERAGE GATE FAILED" in capsys.readouterr().err

    def test_a_missing_report_is_an_error_not_a_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DECILE_TEST_DATABASE_URL", "postgresql+asyncpg://x/y")
        assert main(["--report", str(tmp_path / "nothing.json")]) == 2

    def test_the_thresholds_are_the_ones_prompt_19_names(self) -> None:
        assert THRESHOLDS == {"decile_core": 90.0, "decile_api": 80.0}
