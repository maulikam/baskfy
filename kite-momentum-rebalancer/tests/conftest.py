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


@pytest.fixture(autouse=True)
def isolate_database(tmp_path, monkeypatch):
    """The same rule as the journal above, for the database — and for the same reason.

    The journal fixture exists because a suite run left 2,434 synthetic orders in the real audit
    record. The database was never given the same protection, and M13's route tests found the gap
    the hard way: every `/analyze` in the suite persisted a plan, so a single run wrote ten plans
    into the desk's real `rebalance_versions`, and the day's testing put ninety-eight synthetic
    plans — for symbols named ALPHA through ECHO — into the ledger the desk trades from.

    Autouse and unconditional. A test that wants a real path can still pass one explicitly; no
    test reaches the production database by omission.
    """
    from app.analytics import db

    monkeypatch.setattr(db, "DB_BACKEND", "sqlite")
    monkeypatch.setattr(db.C, "DB_PATH", str(tmp_path / "portfolio.db"))


@pytest.fixture(autouse=True)
def isolate_shared_read_limits(monkeypatch):
    """The same rule again, for SW21's shared Kite read limiter.

    `DeskLimits()` with no argument takes the process's shared spacers, which on any machine
    where `BASKFY_REDIS_URL` is set means a real Redis and a departure clock shared with whatever
    else is using that server — so a suite run could make the desk on the next desk wait, and a
    burst test could measure a queue it did not create.

    Autouse and unconditional, like the two above. A test that wants the shared path builds its
    spacers explicitly and passes them in; `tests/test_kite_limits.py` is the one that does.
    """
    from app.core import kite_limits

    kite_limits.reset_shared_spacers()
    monkeypatch.setattr(kite_limits, "shared_spacers", dict)
    yield
    kite_limits.reset_shared_spacers()


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
