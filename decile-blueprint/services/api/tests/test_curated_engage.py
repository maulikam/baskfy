"""SC9 engagement API — pending actions + updates; dismiss/resolve; no orders."""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from baskfy_api.app import create_app
from baskfy_api.routers import curated_engage
from baskfy_api.routers.curated_engage import (
    PendingActionListOut,
    PendingActionMutationOut,
    UpdatePostListOut,
    _body_for,
    _title_for,
    dismiss_pending_action,
    list_pending_actions,
    list_update_posts,
    resolve_pending_action,
)
from baskfy_core.models import CbPendingAction


def test_openapi_exposes_pending_actions_and_updates() -> None:
    paths = create_app().openapi()["paths"]
    assert "/api/v1/cb/pending-actions" in paths
    assert "/api/v1/cb/updates" in paths
    assert "/api/v1/cb/pending-actions/{action_id}/dismiss" in paths
    assert "/api/v1/cb/pending-actions/{action_id}/resolve" in paths


def test_engage_router_uses_pending_action_model() -> None:
    source = inspect.getsource(curated_engage)
    assert "CbPendingAction" in source
    assert "dismissed_at" in source
    assert "resolved_at" in source
    for forbidden in ("place_order", "OrderGateway", "execute"):
        assert forbidden not in source


def test_title_and_body_prefer_payload_then_type() -> None:
    action = MagicMock(spec=CbPendingAction)
    action.type = "REBALANCE_AVAILABLE"
    action.payload = {"title": "Apply v3", "body": "Two names changed"}
    assert _title_for(action) == "Apply v3"
    assert _body_for(action) == "Two names changed"

    action.payload = {}
    assert "Rebalance" in _title_for(action)
    assert _body_for(action) is None


@pytest.mark.asyncio
async def test_list_pending_actions_filters_to_open_sole_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    open_row = MagicMock(spec=CbPendingAction)
    open_row.id = 1
    open_row.type = "DRIFT"
    open_row.payload = {"title": "Fix RELIANCE"}
    open_row.created_at = MagicMock()

    scalar_result = MagicMock()
    scalar_result.all = MagicMock(return_value=[open_row])
    session = AsyncMock()
    session.scalars = AsyncMock(return_value=scalar_result)
    principal = MagicMock(user_id=9)

    async def _sole(_session: object, _uid: int | None) -> int:
        return 42

    monkeypatch.setattr(curated_engage, "scoped_sole_user_id", _sole)

    result = await list_pending_actions(session, principal)
    assert isinstance(result, PendingActionListOut)
    assert result.count == 1
    assert result.items[0].title == "Fix RELIANCE"
    session.scalars.assert_awaited()


@pytest.mark.asyncio
async def test_list_updates_for_sole_user(monkeypatch: pytest.MonkeyPatch) -> None:
    post = MagicMock()
    post.id = 7
    post.title = "Version 2"
    post.body_md = "Added TCS"
    post.published_at = MagicMock()
    post.source = "ENGINE"
    post.basket_id = 1
    post.manager_id = None

    scalar_result = MagicMock()
    scalar_result.all = MagicMock(return_value=[post])
    session = AsyncMock()
    session.scalars = AsyncMock(return_value=scalar_result)
    principal = MagicMock(user_id=9)

    async def _sole(_session: object, _uid: int | None) -> int:
        return 42

    monkeypatch.setattr(curated_engage, "scoped_sole_user_id", _sole)

    result = await list_update_posts(session, principal)
    assert isinstance(result, UpdatePostListOut)
    assert result.count == 1
    assert result.items[0].title == "Version 2"
    assert result.items[0].source == "ENGINE"


@pytest.mark.asyncio
async def test_dismiss_and_resolve_stamp_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action = MagicMock(spec=CbPendingAction)
    action.id = 3
    action.type = "SIP_DUE"
    action.user_id = 42
    action.dismissed_at = None
    action.resolved_at = None

    session = AsyncMock()
    session.get = AsyncMock(return_value=action)
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    principal = MagicMock(user_id=9)

    async def _sole(_session: object, _uid: int | None) -> int:
        return 42

    monkeypatch.setattr(curated_engage, "scoped_sole_user_id", _sole)

    dismissed = await dismiss_pending_action(3, session, principal)
    assert isinstance(dismissed, PendingActionMutationOut)
    assert action.dismissed_at is not None

    action.dismissed_at = None
    action.resolved_at = None
    resolved = await resolve_pending_action(3, session, principal)
    assert isinstance(resolved, PendingActionMutationOut)
    assert action.resolved_at is not None
