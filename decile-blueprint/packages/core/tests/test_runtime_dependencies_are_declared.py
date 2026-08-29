"""Every module-scope import in this package must be a *runtime* dependency of this package.

The failure this catches is invisible in development and fatal in production. `score.py` and
`basket.py` import pandas at module scope, but pandas was declared only in the root project's
`[dependency-groups]` — the dev group. Every developer, every CI run and every test had it
installed, so nothing ever failed. The production image installs runtime dependencies only.

The first nightly pipeline run that ever got past the quality gate died at step 11 on
`ModuleNotFoundError: No module named 'pandas'`, after nine days of the product serving a stale
session for unrelated reasons. The dependency had been mis-declared the whole time and no test
could see it, because the test environment is exactly the environment where it is present.

docs/02 §"The decision in one table" permits it — "Polars (primary) + NumPy; pandas only at
boundaries" — so this is about *where it is declared*, not whether it is allowed.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PACKAGE_ROOT / "src" / "baskfy_core"
PYPROJECT = PACKAGE_ROOT / "pyproject.toml"

#: Distributions whose import name differs from the name pip installs.
IMPORT_TO_DISTRIBUTION = {"dateutil": "python-dateutil", "yaml": "pyyaml"}

#: Imported at module scope but deliberately not declared: they ship with CPython.
STANDARD_LIBRARY = set(sys.stdlib_module_names)

#: This package's own namespace.
OWN = {"baskfy_core"}


def declared_runtime_dependencies() -> set[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    names: set[str] = set()
    for spec in data["project"]["dependencies"]:
        # "sqlalchemy[asyncio]>=2.0.36" -> "sqlalchemy"
        name = spec.split(">=")[0].split("==")[0].split("[")[0].split("<")[0].strip()
        names.add(name.lower().replace("_", "-"))
    return names


def module_scope_imports() -> dict[str, set[str]]:
    """Top-level imports per file. Function-scope imports are excluded deliberately.

    An import inside a function is a *conditional* dependency — the module loads without it, and
    the code path that needs it may never run in a given deployment. A module-scope import is
    unconditional: if the package imports, the distribution must be installed.
    """
    found: dict[str, set[str]] = {}
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:  # module scope only
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.setdefault(alias.name.split(".")[0], set()).add(path.name)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.setdefault(node.module.split(".")[0], set()).add(path.name)
    return found


def test_every_module_scope_import_is_a_declared_runtime_dependency() -> None:
    declared = declared_runtime_dependencies()
    undeclared: dict[str, set[str]] = {}

    for module, files in module_scope_imports().items():
        if module in STANDARD_LIBRARY or module in OWN:
            continue
        distribution = IMPORT_TO_DISTRIBUTION.get(module, module).lower().replace("_", "-")
        if distribution not in declared:
            undeclared[module] = files

    assert not undeclared, (
        "imported at module scope but not a runtime dependency of packages/core — this is "
        "invisible in dev and fatal in the production image: "
        + "; ".join(f"{m} (in {', '.join(sorted(f))})" for m, f in sorted(undeclared.items()))
    )


def test_pandas_specifically_is_declared() -> None:
    """The one that actually broke, pinned by name so a refactor cannot quietly undo it."""
    assert "pandas" in declared_runtime_dependencies()
