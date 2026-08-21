"""Nothing in the live desk may import the frozen options lab (M6).

The subsystem was disconnected by deferring three module-scope imports behind the existing
`OPTIONS_ENABLED` gate, not by deleting them — a thaw is a `git mv` and nothing else. That
arrangement is easy to undo by accident: one convenient top-level import and the equity desk
stops booting without an options lab that is deliberately absent.

So this asserts the boundary by scanning the source, the same way the screener's
`test_no_escape_hatches.py` asserts its own rules. A grep is not a proof of purity, but it is
a proof that nobody wrote the obvious mistake.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

APP = pathlib.Path(__file__).resolve().parent.parent / "app"
SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"

#: Import targets that only exist in frozen/strangle/.
#:
#: NOT the whole `strategies.strangle` package: four of its twenty modules stayed in the live
#: tree because the equity desk depends on them for the NSE trading calendar — see that
#: package's __init__ and docs/DECISIONS-MERGE.md M6.2. What froze is the strategy, so the
#: strategy modules are what may not be imported at module scope.
FROZEN_STRANGLE_MODULES = (
    "adjust", "allocation", "attribution", "book", "calibrate", "fills_live", "fills_paper",
    "journal", "levels", "live", "market", "rules", "selection", "session", "sizing", "state",
)
FROZEN = tuple(f"strategies.strangle.{m}" for m in FROZEN_STRANGLE_MODULES) + (
    "strategies.options", "analytics.options_view",
)


def _module_level_imports(path: pathlib.Path) -> list[str]:
    """Every module scope import in a file, as dotted text. Function-local ones are fine."""
    tree = ast.parse(path.read_text(), filename=str(path))
    out: list[str] = []
    for node in tree.body:                                   # body == module scope only
        if isinstance(node, ast.Import):
            out.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            out.append(("." * node.level) + (node.module or ""))
    return out


def _sources() -> list[pathlib.Path]:
    return sorted(p for d in (APP, SCRIPTS) for p in d.rglob("*.py")
                  if "__pycache__" not in p.parts)


@pytest.mark.parametrize("path", _sources(), ids=lambda p: str(p.name))
def test_no_module_level_import_of_the_frozen_subsystem(path: pathlib.Path) -> None:
    for imported in _module_level_imports(path):
        for frozen in FROZEN:
            assert frozen not in imported, (
                f"{path.name} imports {imported!r} at module scope. The options lab is frozen "
                f"(frozen/strangle/); import it inside the function that needs it, behind the "
                f"OPTIONS_ENABLED gate, so the equity desk still boots without it."
            )


def test_the_desk_boots_and_the_options_routes_are_absent() -> None:
    """The behaviour the boundary exists to protect."""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    assert client.get("/").status_code == 200
    assert client.get("/ops").status_code == 200
    for path in ("/options", "/options/data"):
        assert client.get(path).status_code == 404, f"{path} should be absent while frozen"


def test_the_operations_registry_offers_no_options_controls_while_frozen() -> None:
    from app.analytics import ops

    assert "Options" not in ops.groups()
    assert not [o for o in ops.all_operations() if o.group == "Options"]
