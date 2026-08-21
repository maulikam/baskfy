"""The house rules from PROMPTS.md §"House rules", enforced rather than trusted.

    "No `# type: ignore`, no `any`, no silently swallowed exceptions."

mypy's own ``disallow_any_explicit`` cannot express this: the pydantic plugin synthesises
``__init__(**data: Any)`` on every model, so the flag fires on generated code we do not own.
Scanning our source directly is narrower and actually enforceable.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

SOURCE_ROOTS = (
    REPO_ROOT / "packages" / "core",
    REPO_ROOT / "packages" / "providers",
    REPO_ROOT / "services" / "api",
    REPO_ROOT / "services" / "worker",
)

EXCLUDED_PARTS = frozenset({".venv", "node_modules", "__pycache__", ".mypy_cache", ".ruff_cache"})

#: This file quotes the very patterns it forbids, so it excludes itself. Nothing else may.
SELF = Path(__file__).resolve()


def python_files() -> list[Path]:
    from_packages = [path for root in SOURCE_ROOTS if root.exists() for path in root.rglob("*.py")]
    # The repo-root conftest patches sockets for the whole suite; it is exactly the sort of file
    # a `type: ignore` creeps into, so it is scanned rather than exempt.
    root_level = [path for path in REPO_ROOT.glob("*.py")]
    return sorted(
        path
        for path in (*from_packages, *root_level)
        if EXCLUDED_PARTS.isdisjoint(path.parts) and path.resolve() != SELF
    )


def _offenders(pattern: re.Pattern[str]) -> list[str]:
    found: list[str] = []
    for path in python_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if pattern.search(line):
                found.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}")
    return found


def test_there_are_python_files_to_scan() -> None:
    """A scan over nothing would pass silently and prove nothing."""
    assert len(python_files()) > 10


def test_no_type_ignore_comments() -> None:
    assert _offenders(re.compile(r"#\s*type:\s*ignore")) == []


def test_no_mypy_or_ruff_blanket_suppressions() -> None:
    """A file-wide `# mypy: ignore-errors` is a `type: ignore` with a wider blast radius."""
    assert _offenders(re.compile(r"#\s*mypy:\s*(ignore-errors|disable)")) == []


def test_no_explicit_any_annotations() -> None:
    """`Any` in an annotation of ours; `object` is almost always the honest alternative."""
    pattern = re.compile(r"(:\s*Any\b|->\s*Any\b|\[\s*Any\b|,\s*Any\s*[\],])")
    assert _offenders(pattern) == []


def test_no_bare_or_broad_except_pass() -> None:
    """A swallowed exception turns a data-quality failure into a silently wrong number."""
    swallowed: list[str] = []
    for path in python_files():
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if not re.match(r"\s*except\b.*:\s*$", line):
                continue
            following = lines[index + 1].strip() if index + 1 < len(lines) else ""
            if following in {"pass", "..."}:
                swallowed.append(f"{path.relative_to(REPO_ROOT)}:{index + 1}: {line.strip()}")
    assert swallowed == []


@pytest.mark.parametrize("name", ["ruff", "mypy", "pytest"])
def test_tooling_is_configured_at_the_workspace_root(name: str) -> None:
    """`make lint` must mean the same thing for every package."""
    config = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f"[tool.{name}" in config
