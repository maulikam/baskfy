"""Dump every environment variable the desk reads, with its default and parse behaviour.

The answer key for ``rust/crates/baskfy-desk/src/settings.rs`` (DECISIONS-RS R8.1). Run with the
desk's venv and the worktree on ``PYTHONPATH``; it never reads ``.env`` (``load_dotenv`` is
replaced by a no-op before ``app.config`` is imported), never connects anywhere, and writes one
JSON file::

    PYTHONPATH=<wt>/kite-momentum-rebalancer:<wt>/decile-blueprint/packages/core/src:... \\
        <main>/kite-momentum-rebalancer/.venv/bin/python tools/parity/desk_settings_dump.py \\
        --out rust/crates/baskfy-desk/fixtures/settings_defaults.json

What it records, per variable:

* ``module`` and ``attrs`` — the module-level names whose value comes from the variable (found by
  walking each module's AST for ``os.getenv("KEY", ...)`` / ``os.environ.get("KEY", ...)``),
  plus any later module-level name computed from one of them (``SWING_EP_PREMARKET_ENABLED``
  from ``_EP_PREMARKET``).
* ``default`` — those attributes' values with the variable unset.
* ``probes`` — the same attributes after a reload with the variable set to each probe string,
  or ``{"error": "<ExceptionType>"}`` when Python refuses it (``int("yes")``).
* Variables read inside a function (``BASKFY_SOLE_USER_ID`` in the monitors, defaulting to
  ``"0"``, not config's ``"1"``) are listed under ``function_level`` with their literal default.

Values are encoded so JSON can carry them: a float that is not finite as ``{"float": "nan"}``,
a tuple as ``{"tuple": [...]}``, a set as ``{"set": [...sorted]}``.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

PROBES = ["", "true", "false", "TRUE", "False", "1", "0", "yes", "7", "2.5", " 3 ", "abc", "a,b",
          "1_000", "-4", "inf", "nan", "1e3"]

#: Modules whose module-level code reads the environment. Function-level reads are found in all
#: of ``app/`` (excluding the frozen strangle tree, which the desk never imports).
MODULE_LEVEL = ["app.config", "app.analytics.db", "app.scan_source", "app.breadth_source",
                "app.telemetry"]


def _encode(value: Any) -> Any:  # noqa: ANN401 - any config value
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else {"float": repr(value)}
    if isinstance(value, tuple):
        return {"tuple": [_encode(v) for v in value]}
    if isinstance(value, (set, frozenset)):
        return {"set": sorted(_encode(v) for v in value)}
    if isinstance(value, list):
        return [_encode(v) for v in value]
    raise TypeError(f"cannot encode {type(value).__name__}")


def _env_key(node: ast.AST) -> str | None:
    """``os.getenv("K", ...)`` or ``os.environ.get("K", ...)`` → ``"K"``."""
    if not isinstance(node, ast.Call) or not node.args:
        return None
    first = node.args[0]
    if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
        return None
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "getenv":
        return first.value
    if (isinstance(func, ast.Attribute) and func.attr == "get"
            and isinstance(func.value, ast.Attribute) and func.value.attr == "environ"):
        return first.value
    return None


def _literal_default(node: ast.Call) -> Any:  # noqa: ANN401
    if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
        return node.args[1].value
    return None


def _module_file(module: str, root: Path) -> Path:
    return root / (module.replace(".", "/") + ".py")


def module_level_vars(module: str, root: Path) -> dict[str, list[str]]:
    """key → module-level attribute names derived from it (directly or one step on)."""
    tree = ast.parse(_module_file(module, root).read_text())
    out: dict[str, list[str]] = {}
    derived: dict[str, set[str]] = {}
    for stmt in tree.body:
        if not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            continue
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        if not names or stmt.value is None:
            continue
        keys = {k for n in ast.walk(stmt.value) if (k := _env_key(n))}
        used = {n.id for n in ast.walk(stmt.value) if isinstance(n, ast.Name)}
        for key in keys:
            out.setdefault(key, []).extend(names)
            for name in names:
                derived.setdefault(name, set()).add(key)
        if not keys:
            for name in used & set(derived):
                for key in derived[name]:
                    out.setdefault(key, []).extend(names)
    return out


def function_level_vars(root: Path, module_keys: set[str]) -> list[dict[str, Any]]:
    rows = []
    for path in sorted((root / "app").rglob("*.py")):
        if "strangle" in path.parts:
            continue
        tree = ast.parse(path.read_text())
        top = {id(n) for s in tree.body if isinstance(s, (ast.Assign, ast.AnnAssign))
               for n in ast.walk(s)}
        for node in ast.walk(tree):
            key = _env_key(node)
            if key is None or id(node) in top:
                continue
            assert isinstance(node, ast.Call)
            module = ".".join(path.relative_to(root).with_suffix("").parts)
            rows.append({"key": key, "module": module, "line": node.lineno,
                         "default_literal": _literal_default(node),
                         "also_module_level": key in module_keys})
    return rows


def snapshot(module: str, attrs: list[str]) -> dict[str, Any]:
    mod = importlib.import_module(module)
    mod = importlib.reload(mod)
    return {a: _encode(getattr(mod, a)) for a in attrs}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    import dotenv  # noqa: PLC0415 - patched before the desk imports it

    setattr(dotenv, "load_dotenv", lambda *a, **k: False)  # never read .env
    import app  # noqa: PLC0415

    root = Path(app.__file__).resolve().parents[1]
    module_vars = {m: module_level_vars(m, root) for m in MODULE_LEVEL}
    every_key = {k for v in module_vars.values() for k in v}
    functions = function_level_vars(root, every_key)
    for key in every_key | {r["key"] for r in functions}:
        os.environ.pop(key, None)

    rows = []
    for module, keys in module_vars.items():
        for key in sorted(keys):
            attrs = list(dict.fromkeys(keys[key]))
            row: dict[str, Any] = {"key": key, "module": module, "attrs": attrs,
                                   "default": snapshot(module, attrs), "probes": {}}
            for probe in PROBES:
                os.environ[key] = probe
                try:
                    row["probes"][probe] = snapshot(module, attrs)
                except Exception as exc:  # noqa: BLE001 - the refusal IS the answer
                    row["probes"][probe] = {"error": type(exc).__name__}
                finally:
                    os.environ.pop(key, None)
            rows.append(row)
    # Leave every module as the defaults built it.
    for module in MODULE_LEVEL:
        importlib.reload(importlib.import_module(module))

    out = {
        "generated_by": "tools/parity/desk_settings_dump.py",
        "python": sys.version.split()[0],
        "probes": PROBES,
        "vars": rows,
        "function_level": functions,
    }
    Path(args.out).write_text(json.dumps(out, indent=1, sort_keys=False, allow_nan=False) + "\n")
    print(f"{len(rows)} module-level variables, {len(functions)} function-level reads -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
