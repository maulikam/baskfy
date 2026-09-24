"""FO12 — the F&O book deployed dark (``docs/fno/06`` FO12: "Deploy … with every money flag
still false"; the options pack's OP15 precedent, ``test_options_monitor_loop.TestTheService``).

* the desk image carries ``fno-monitor-loop`` and compose runs it as ``fno-monitor`` from the desk
  block, so the monitor and the page that confirms read the same flags;
* every FO money flag is named in that block with a ``false`` default (a variable compose does not
  name never reaches the container, and one it names can be flipped only in
  ``.env.staging.compose``), ``OPTIONS_ENABLED``/``INTRADAY_ENABLED`` stay pinned ``"false"``;
* no ``BASKFY_FNO_*AUTO*`` name anywhere in compose (``02`` Track B);
* a deploy restarts ``fno-monitor``, ``ship.sh`` expects it running and runs ``verify-fno.sh``.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "decile-blueprint" / "infra" / "docker" / "compose.prod.yml"
DOCKERFILE = REPO / "decile-blueprint" / "infra" / "docker" / "Dockerfile.desk"
DEPLOY = REPO / "tools" / "deploy"
MONEY_FLAGS = (
    "BASKFY_FNO_CARRY_ENABLED",
    "BASKFY_FNO_F1_EXECUTION_ENABLED",
    "BASKFY_FNO_F2_EXECUTION_ENABLED",
)


def _services() -> dict[str, dict[str, object]]:
    services = yaml.safe_load(COMPOSE.read_text())["services"]
    assert isinstance(services, dict)
    return services


def test_the_image_carries_the_loop() -> None:
    text = DOCKERFILE.read_text()
    assert "/usr/local/bin/fno-monitor-loop" in text
    assert "exec python -m scripts.fno_monitor_loop" in text


def test_compose_runs_the_monitor_from_the_desk_block_with_money_flags_false() -> None:
    services = _services()
    service = services["fno-monitor"]
    assert service["command"] == ["fno-monitor-loop"]
    for name in ("desk", "fno-monitor"):
        env = services[name]["environment"]
        assert isinstance(env, dict)
        for flag in MONEY_FLAGS:
            assert env[flag] == f"${{{flag}:-false}}", (name, flag)
        assert env["BASKFY_FNO_MONITOR_ENABLED"] == "${BASKFY_FNO_MONITOR_ENABLED:-false}"
        assert env["OPTIONS_ENABLED"] == "false" and env["INTRADAY_ENABLED"] == "false"
        assert env["DRY_RUN"] == "${BASKFY_DESK_DRY_RUN:-true}"


def test_no_fno_auto_execute_variable_in_compose() -> None:
    assert re.search(r"FNO\w*AUTO", COMPOSE.read_text()) is None


def test_a_deploy_restarts_it_and_ship_expects_it_and_verifies_it() -> None:
    deploy = (DEPLOY / "deploy-swing.sh").read_text()
    lines = [ln for ln in deploy.splitlines() if ln.strip().startswith("RESTART_SERVICES=")]
    assert lines and all(" fno-monitor " in ln for ln in lines)  # both, KEEP_MONITOR or not
    ship = (DEPLOY / "ship.sh").read_text()
    assert ship.index("verify-options.sh") < ship.index("bash tools/deploy/verify-fno.sh")
    running = re.findall(r'running=(\d+)"\*\)', ship)
    assert running == [str(len(_expected_running()))]
    assert (DEPLOY / "verify-fno.sh").exists()


def _expected_running() -> list[str]:
    """The long-running services ``up -d`` starts: every service not behind a profile."""
    return [
        name
        for name, spec in _services().items()
        if isinstance(spec, dict) and not spec.get("profiles")
    ]
