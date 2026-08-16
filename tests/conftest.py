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
