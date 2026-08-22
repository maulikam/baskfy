"""The two broker faces must not overlap (M16, P3.6).

docs/03 §3b: "a user's access token must never fetch universe data, and the system token must
never place an order." A token is an opaque string, so nothing can check which one a caller holds.
What can be checked is that the two clients do not share a surface — which is what makes the
mistake impossible to make by accident rather than merely against the rules.

It lives in the desk's suite rather than the execution package's because it is the one environment
where **both** clients are importable: the desk's venv carries `baskfy_providers` (the system face)
and `app.kite_client` (the account face). A copy in `packages/execution/tests` would skip the half
that matters and then drift.
"""
from __future__ import annotations

import pytest

from baskfy_execution.brokers import MARKET_DATA_ONLY, TRADING_ONLY


def _market_data_client() -> object:
    from baskfy_providers.kite import KiteProvider

    return KiteProvider


def _trading_client() -> object:
    pytest.importorskip("kiteconnect")
    try:
        from app.kite_client import Kite       # the desk, when it is on the path
    except ModuleNotFoundError:                # pragma: no cover - running outside the desk
        pytest.skip("the desk's trading client is not importable from here")
    return Kite


@pytest.mark.parametrize("method", sorted(TRADING_ONLY))
def test_the_system_credentialled_client_cannot_place_or_hold_anything(method: str) -> None:
    """KiteProvider feeds the pipeline. If it grows `place_order`, the system token can trade."""
    assert not hasattr(_market_data_client(), method), (
        f"KiteProvider has {method}(): the SYSTEM's credentials can now reach an account "
        f"operation. Ingestion and trading are different apps, different tokens, different "
        f"blast radius."
    )


@pytest.mark.parametrize("method", sorted(MARKET_DATA_ONLY))
def test_the_account_credentialled_client_cannot_fetch_the_universe(method: str) -> None:
    """The desk's client acts on one book. If it grows `daily_bars`, a user's token can be
    used to run the pipeline — which is a per-user rate limit spent on shared work, and at P4
    a user paying for everyone else's ingestion."""
    assert not hasattr(_trading_client(), method), (
        f"the desk's Kite client has {method}(): an ACCOUNT's token can now fetch universe "
        f"data. That belongs to the system's own app."
    )


def test_the_two_surfaces_are_disjoint() -> None:
    assert not (MARKET_DATA_ONLY & TRADING_ONLY)
