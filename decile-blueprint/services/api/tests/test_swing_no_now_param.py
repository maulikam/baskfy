"""AUDIT 2.15 — PATCH /swing/config refuses a client-supplied ``now`` clock."""

from __future__ import annotations

import inspect

from baskfy_api.routers import swing


def test_patch_config_does_not_accept_a_now_parameter() -> None:
    source = inspect.getsource(swing.patch_config)
    assert "now: dt.datetime" not in source
    assert "now: datetime" not in source
