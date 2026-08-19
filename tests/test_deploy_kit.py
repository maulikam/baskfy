"""The deployment kit.

It exists because the desk needs a static outbound IP — Kite's order allowlist cannot
hold a residential address that changed from 103.238.14.245 to 49.43.34.118 inside a day
— and because the 09:20 and 18:30 jobs only ran when a laptop happened to be awake.

These tests keep the kit honest against the app it deploys. A unit that points at a path
the sync script does not create, or a schedule that quietly drifts from the one the
laptop ran, fails on a morning nobody is watching.
"""
from __future__ import annotations

import pathlib
import re

import pytest

DEPLOY = pathlib.Path("deploy")
UNITS = sorted((DEPLOY / "systemd").glob("*.service"))
TIMERS = sorted((DEPLOY / "systemd").glob("*.timer"))


def unit(name: str) -> str:
    return (DEPLOY / "systemd" / name).read_text()


# =====================================================================================
# the interface must not be reachable from the internet
# =====================================================================================
def test_the_web_service_binds_to_loopback_only():
    """It places real orders behind a confirmation gate. Binding 0.0.0.0 would put that
    gate on the public internet, and a static IP is exactly what makes a box findable."""
    exec_line = next(l for l in unit("momentum-web.service").splitlines()
                     if l.startswith("ExecStart="))
    assert "--host 127.0.0.1" in exec_line
    assert "0.0.0.0" not in exec_line


def test_the_firewall_opens_nothing_but_ssh():
    boot = (DEPLOY / "bootstrap.sh").read_text()
    assert "default deny incoming" in boot
    allows = re.findall(r"ufw allow (\S+)", boot)
    assert allows == ["OpenSSH"], f"the box would also accept {allows}"


def test_no_unit_exposes_the_application_port():
    for f in UNITS:
        assert "8420" not in f.read_text() or "127.0.0.1" in f.read_text(), f.name


# =====================================================================================
# the schedule the laptop ran
# =====================================================================================
def test_the_daily_job_still_runs_after_the_close_on_weekdays():
    """18:30 IST. Before 15:30 the EOD snapshot cannot exist, and kc.margins() has no
    history to backfill it from."""
    t = unit("momentum-daily.timer")
    assert "OnCalendar=Mon..Fri 18:30" in t


def test_the_straddle_job_still_runs_just_after_the_open():
    """09:20 IST — after 09:15, when an option first has a two-sided quote."""
    assert "OnCalendar=Mon..Fri 09:20" in unit("strangle-collect@.timer")


def test_a_missed_snapshot_is_caught_up_but_a_missed_straddle_is_not():
    """Opposite answers, for a reason. A late snapshot is still that day's snapshot. A
    straddle recorded hours late is a different point on the decay curve, and the bands
    are built on opening prints — mislabelling one is worse than missing it."""
    assert "Persistent=true" in unit("momentum-daily.timer")
    assert "Persistent=false" in unit("strangle-collect@.timer")


def test_every_configured_instrument_is_collected():
    from app.strategies.strangle import instruments as INS
    install = (DEPLOY / "install-units.sh").read_text()
    for slug in INS.all_slugs():
        assert slug in install, f"{slug} would never be collected on the box"


def test_not_logged_in_is_not_treated_as_a_unit_failure():
    """Exit 2 means a person has to log in to Kite. That is recoverable and expected, and
    flagging the unit failed for it would train you to ignore the flag."""
    assert "SuccessExitStatus=0 2" in unit("strangle-collect@.service")


# =====================================================================================
# the kit is internally consistent
# =====================================================================================
def test_units_run_the_interpreter_the_installer_creates():
    """There is no pyproject here, so `uv run` has nothing to resolve; requirements.txt
    and a plain venv are the manifest, exactly as on the laptop."""
    install = (DEPLOY / "install-units.sh").read_text()
    assert "uv pip install -r requirements.txt" in install
    for f in UNITS:
        for line in f.read_text().splitlines():
            if line.startswith("ExecStart="):
                assert "/.venv/bin/" in line, f"{f.name}: {line}"


def test_every_unit_path_matches_where_sync_puts_the_repo():
    sync = (DEPLOY / "sync.sh").read_text()
    remote = re.search(r'REMOTE="([^"]+)"', sync).group(1)
    for f in UNITS:
        assert f"/home/desk/{remote}" in f.read_text(), f.name


def test_the_units_and_the_box_agree_on_the_timezone():
    """Every schedule in this system is written in IST. A box left on UTC would run the
    morning collect five hours into the session."""
    assert "Asia/Kolkata" in (DEPLOY / "bootstrap.sh").read_text()
    for f in UNITS:
        assert "TZ=Asia/Kolkata" in f.read_text(), f.name


def test_state_is_not_clobbered_by_a_routine_sync():
    """After cutover the box keeps the record and the laptop's copy is a stale fork."""
    sync = (DEPLOY / "sync.sh").read_text()
    assert "--ignore-existing" in sync
    assert "--force-state" in sync, "there must be a deliberate way to overwrite"


@pytest.mark.parametrize("script", ["bootstrap.sh", "sync.sh", "install-units.sh"])
def test_the_scripts_are_executable_and_fail_fast(script):
    p = DEPLOY / script
    assert p.stat().st_mode & 0o111, f"{script} is not executable"
    assert "set -euo pipefail" in p.read_text(), f"{script} would continue past an error"


# =====================================================================================
# the box, hardened
# =====================================================================================
def test_ssh_is_keys_only_and_root_cannot_log_in():
    boot = (DEPLOY / "bootstrap.sh").read_text()
    for rule in ("PasswordAuthentication no", "PermitRootLogin no"):
        assert rule in boot, rule


def test_it_refuses_to_lock_you_out():
    """Disabling password auth on a box with no authorized_keys produces a machine nobody
    can reach. The script checks first and says so instead."""
    boot = (DEPLOY / "bootstrap.sh").read_text()
    assert "authorized_keys" in boot
    assert "lock yourself out" in boot


def test_the_box_patches_itself_and_resists_brute_force():
    boot = (DEPLOY / "bootstrap.sh").read_text()
    assert "unattended-upgrades" in boot and "fail2ban" in boot


def test_credentials_are_not_left_world_readable():
    boot = (DEPLOY / "bootstrap.sh").read_text()
    assert "chmod 600 .env" in boot
    # and the app does not rely on the deploy step remembering
    from app.core import websec as W
    assert ".env" in W.SECRET_FILES and "data/.kite_token.json" in W.SECRET_FILES


def test_the_readme_states_the_password_must_be_set_on_a_hosted_box():
    readme = (DEPLOY / "README.md").read_text()
    assert "DESK_PASSWORD" in readme
    assert "empty by default" in readme
