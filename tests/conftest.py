"""Shared test isolation.

The order journal is the system's audit record: every placement, dry run, guard refusal
and error the gateway has ever performed. It is what the options page reads to show that
nothing has been traded, and what anyone would read after an incident to reconstruct what
happened.

Until this fixture existed the suite appended to the REAL journal at
data/outputs/orders_journal.jsonl. A run left 2,434 lines of synthetic orders in it —
symbols S00-S07, dry runs, and eighteen overnight-option refusals for a contract that was
never traded. An audit record that contains events which did not happen cannot be used as
evidence of anything, which is precisely the job it exists to do.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_order_journal(tmp_path, monkeypatch):
    """Point the gateway's journal at a per-test file.

    Autouse and unconditional: a test that wants to assert on journal contents can still
    monkeypatch it to its own path, but no test can reach the production file by omission.
    """
    import app.core.gateway as gateway
    monkeypatch.setattr(gateway, "JOURNAL", str(tmp_path / "orders_journal.jsonl"))


# =====================================================================================
# Browsers send Origin on every POST, including same-origin ones, and core/websec.py
# requires it — that is what stops a form on another site posting to the desk while the
# tunnel is open. Route tests are about routes, so the client sends the header the way a
# browser would; the protection itself is tested directly in tests/test_websec.py, which
# asserts what happens when the header is absent, wrong, or the Host is a stranger.
# =====================================================================================
import pytest as _pytest
from fastapi.testclient import TestClient as _TestClient

_ORIGIN = {"Origin": "http://testserver:8420"}


@_pytest.fixture(autouse=True)
def _same_origin_by_default(monkeypatch):
    original = _TestClient.__init__

    def patched(self, app, *a, **kw):
        headers = {**_ORIGIN, **(kw.pop("headers", None) or {})}
        original(self, app, *a, headers=headers, **kw)

    monkeypatch.setattr(_TestClient, "__init__", patched)
