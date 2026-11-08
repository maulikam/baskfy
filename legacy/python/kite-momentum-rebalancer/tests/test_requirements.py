"""requirements.txt has to be able to rebuild this environment.

It could not. `httpx2==0.29.0` was pinned; that version has never existed on PyPI, and
2.10.0 was what the venv actually held. Nothing noticed for as long as the venv was never
rebuilt — and the first rebuild was on the production box, mid-deploy.

The file's own header says the pins exist so "a trading system should not change behaviour
because a transitive release shipped". A pin that cannot be installed does not deliver
that; it just moves the failure to the least convenient moment.
"""
from __future__ import annotations

import importlib.metadata as md
import pathlib
import re

import pytest

REQ = pathlib.Path("requirements.txt")

# Distribution name -> the name you import, where they differ.
IMPORT_NAME = {"python-multipart": "multipart", "pyyaml": "yaml",
               "python-dateutil": "dateutil", "beautifulsoup4": "bs4"}


# A requirement line may legitimately carry an extras group and an environment marker:
#     uvicorn[standard]==0.52.3
#     uvloop==0.22.1; sys_platform != 'win32'
# Both are still exact pins, and a parser that cannot read them reports the file as loose
# when it is not.
PIN = re.compile(r"^([A-Za-z0-9._-]+)(?:\[[^\]]+\])?==([A-Za-z0-9._+-]+)\s*(?:;.*)?$")


def pins() -> list[tuple[str, str]]:
    out = []
    for line in REQ.read_text().splitlines():
        m = PIN.match(line.split("#")[0].strip())
        if m:
            out.append((m.group(1), m.group(2)))
    return out


def test_the_file_is_fully_pinned():
    """Ranges let a transitive release land mid-session. Starlette 1.6 did exactly that
    and broke the webview with a 500."""
    loose = [l.split("#")[0].strip() for l in REQ.read_text().splitlines()
             if l.split("#")[0].strip() and not PIN.match(l.split("#")[0].strip())]
    assert not loose, f"unpinned: {loose}"


@pytest.mark.parametrize("name,version", pins())
def test_every_pin_matches_what_is_installed(name, version):
    """The cheap version of "can this file rebuild the venv". A pin that disagrees with
    the running environment is either wrong, or the environment has drifted — and both
    mean the next clean install produces something you have not tested."""
    try:
        have = md.version(name)
    except md.PackageNotFoundError:
        pytest.fail(f"{name} is pinned at {version} but is not installed at all")
    assert have == version, (
        f"{name}: requirements says {version}, the venv has {have}. "
        "A clean install would not reproduce this environment.")


def test_nothing_the_app_imports_is_missing_from_the_file():
    """The other direction: a dependency that works locally because it arrived as someone
    else's transitive requirement, and vanishes when that changes."""
    pinned = {n.lower().replace("_", "-") for n, _v in pins()}
    for dist in ("fastapi", "uvicorn", "jinja2", "pandas", "numpy", "kiteconnect",
                 "pyyaml", "httpx2"):
        assert dist in pinned, f"{dist} is used but not pinned"
