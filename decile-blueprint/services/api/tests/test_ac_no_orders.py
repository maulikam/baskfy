"""Tree-2 leaf 2.7 — create API + SIP Beat task must never grow an order path.

Fails clearly if sibling leaves 2.1 / 2.2 have not landed their modules yet.
When the files exist, asserts they name no OrderGateway / place_order / confirm=true.
"""

from __future__ import annotations

from pathlib import Path

import pytest

FORBIDDEN = ("OrderGateway", "place_order", "confirm=true")

# Resolved from this test file so the check does not depend on cwd.
# .../services/api/tests/this.py → parents[2] == services/
_SERVICES = Path(__file__).resolve().parents[2]
CREATE_ROUTER = _SERVICES / "api" / "src" / "baskfy_api" / "routers" / "curated_create.py"
SIP_TASK = _SERVICES / "worker" / "src" / "baskfy_worker" / "tasks" / "curated_sip.py"


@pytest.mark.parametrize(
    "path",
    [
        pytest.param(CREATE_ROUTER, id="curated_create"),
        pytest.param(SIP_TASK, id="curated_sip"),
    ],
)
def test_ac_create_and_sip_sources_exist(path: Path) -> None:
    assert path.is_file(), (
        f"missing AC surface {path.name} at {path} — "
        "leaf 2.1 (SIP Beat) / 2.2 (Create API) must land before integrity can pass"
    )


@pytest.mark.parametrize(
    "path",
    [
        pytest.param(CREATE_ROUTER, id="curated_create"),
        pytest.param(SIP_TASK, id="curated_sip"),
    ],
)
def test_ac_create_and_sip_source_names_no_execution(path: Path) -> None:
    if not path.is_file():
        pytest.fail(
            f"cannot scan missing {path.name} for {FORBIDDEN!r} — "
            "sibling leaf did not write the module (do not treat as clean)"
        )
    source = path.read_text(encoding="utf-8")
    for token in FORBIDDEN:
        assert token not in source, f"{path.name} must not contain {token!r}"
