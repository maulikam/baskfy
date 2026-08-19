"""Exit codes are how a scheduled job is triaged.

`launchctl list` shows a name and a number. If every failure is 1, a morning nobody
logged in looks exactly like a broken chain or a stale instrument dump — and those have
different remedies. Only one failure is fixed by a human opening a browser, so only one
gets its own code.

    0  did its work, or decided there was nothing to do
    2  NOT LOGGED IN — recoverable, by a person, while the market is open
    1  everything else
"""
from __future__ import annotations

import ast
import contextlib
import io
import pathlib

import pytest

SCRIPTS = sorted(pathlib.Path("scripts").glob("*.py"))


def _code(payload: dict) -> int:
    import scripts.strangle as S
    with contextlib.redirect_stdout(io.StringIO()):
        return S._out(payload)


# =====================================================================================
# the runner that got it wrong
# =====================================================================================
def test_not_logged_in_exits_two():
    """This is the fix. scripts/strangle.py funnelled every status through one helper
    that returned 1 for anything not OK, so the 09:20 collect job reported the same
    number whether the token had expired overnight or the chain was malformed."""
    assert _code({"status": "AUTH_REQUIRED", "login_url": "https://..."}) == 2


@pytest.mark.parametrize("status", ["OK", "COLLECTED", "SKIPPED"])
def test_a_session_that_did_its_work_exits_zero(status):
    """SKIPPED counts: a day the gates vetoed is a day the strategy decided about."""
    assert _code({"status": status}) == 0


@pytest.mark.parametrize("status", ["NO_ASP", "STARTUP_REFUSED", "NO_DEPTH",
                                    "CONFIG_MISMATCH", "SIZING_REFUSED"])
def test_a_real_failure_still_exits_one(status):
    assert _code({"status": status}) == 1


def test_an_unknown_status_is_a_failure_not_a_success():
    """The safe direction. A status added later must not be read as a clean run."""
    assert _code({"status": "SOMETHING_NEW"}) == 1
    assert _code({}) == 1


# =====================================================================================
# and every other entry point agrees
# =====================================================================================
def _checks_auth(src: str) -> bool:
    return "is_authed" in src


def _has_exit_two(tree: ast.AST) -> bool:
    """A literal 2 returned, or raised as SystemExit."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Constant) \
                and node.value.value == 2:
            return True
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) \
                and getattr(node.exc.func, "id", "") == "SystemExit" \
                and any(isinstance(a, ast.Constant) and a.value == 2 for a in node.exc.args):
            return True
    return False


def test_every_script_that_can_hit_an_expired_token_exits_two():
    """Six of seven already did; scripts/strangle.py was the exception, and it is the one
    that runs unattended every morning."""
    missing = []
    for f in SCRIPTS:
        src = f.read_text()
        if not _checks_auth(src):
            continue
        tree = ast.parse(src)
        # strangle.py routes through _out, which is covered by the tests above
        if f.name == "strangle.py":
            assert "AUTH_EXIT = 2" in src, "strangle.py lost its auth exit code"
            continue
        if not _has_exit_two(tree):
            missing.append(f.name)
    assert not missing, f"{missing} cannot be triaged from launchctl output"


def test_the_scripts_that_check_auth_are_the_ones_we_think():
    """So a new unattended script cannot quietly join the schedule without a code."""
    checkers = {f.name for f in SCRIPTS if _checks_auth(f.read_text())}
    assert "strangle.py" in checkers and "daily.py" in checkers and "autorun.py" in checkers
