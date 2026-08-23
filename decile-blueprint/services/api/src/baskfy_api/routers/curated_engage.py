"""Curated engagement — pending actions + update posts for the sole user (SC9).

``GET /cb/pending-actions`` and ``GET /cb/updates`` feed the home / investments slots.
Dismiss and resolve stamp ``dismissed_at`` / ``resolved_at``; they never place orders
(Track C / PACK.2).
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select

from baskfy_api.auth import AuthenticatedDep
from baskfy_api.curated_tenant import scoped_sole_user_id
from baskfy_api.db import SessionDep
from baskfy_api.problems import not_found
from baskfy_core.models import CbPendingAction, CbUpdatePost

router = APIRouter(tags=["curated-engage"])

_TITLE_FOR_TYPE: dict[str, str] = {
    "DRIFT": "Incorrect holdings — Fix now",
    "REBALANCE_AVAILABLE": "Rebalance update available",
    "SIP_DUE": "SIP due",
    "GENERIC": "Action needed",
}


class PendingActionOut(BaseModel):
    id: int
    type: str
    title: str
    body: str | None = None
    # ``object``, not ``Any`` (CLAUDE.md house rule 3): this mirrors the JSONB
    # ``cb_pending_action.payload`` verbatim, and its cells are genuinely unknown here —
    # each action type documents its own keys. Nothing in this router indexes into them.
    payload: dict[str, object] = Field(default_factory=dict)
    created_at: dt.datetime


class PendingActionListOut(BaseModel):
    items: list[PendingActionOut]
    count: int


class UpdatePostOut(BaseModel):
    id: int
    title: str
    body_md: str
    published_at: dt.datetime
    source: str
    basket_id: int | None = None
    manager_id: int | None = None


class UpdatePostListOut(BaseModel):
    items: list[UpdatePostOut]
    count: int


class PendingActionMutationOut(BaseModel):
    id: int
    type: str
    dismissed_at: dt.datetime | None = None
    resolved_at: dt.datetime | None = None


def _title_for(action: CbPendingAction) -> str:
    payload = action.payload if isinstance(action.payload, dict) else {}
    raw = payload.get("title")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return _TITLE_FOR_TYPE.get(action.type, "Action needed")


def _body_for(action: CbPendingAction) -> str | None:
    payload = action.payload if isinstance(action.payload, dict) else {}
    for key in ("body", "body_md", "message", "summary"):
        raw = payload.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def _pending_out(action: CbPendingAction) -> PendingActionOut:
    payload = action.payload if isinstance(action.payload, dict) else {}
    return PendingActionOut(
        id=action.id,
        type=action.type,
        title=_title_for(action),
        body=_body_for(action),
        payload=dict(payload),
        created_at=action.created_at,
    )


@router.get("/cb/pending-actions", response_model=PendingActionListOut)
async def list_pending_actions(
    session: SessionDep,
    principal: AuthenticatedDep,
) -> PendingActionListOut:
    """Open pending actions for the sole tenant (not dismissed, not resolved)."""
    user_id = await scoped_sole_user_id(session, principal.user_id)
    rows = (
        await session.scalars(
            select(CbPendingAction)
            .where(
                CbPendingAction.user_id == user_id,
                CbPendingAction.dismissed_at.is_(None),
                CbPendingAction.resolved_at.is_(None),
            )
            .order_by(CbPendingAction.created_at.desc())
        )
    ).all()
    items = [_pending_out(row) for row in rows]
    return PendingActionListOut(items=items, count=len(items))


@router.get("/cb/updates", response_model=UpdatePostListOut)
async def list_update_posts(
    session: SessionDep,
    principal: AuthenticatedDep,
) -> UpdatePostListOut:
    """Manager / engine update posts the sole user sees on home."""
    # Auth + sole-tenant gate: feed is global, but only a signed-in sole user reads it in Track A.
    await scoped_sole_user_id(session, principal.user_id)
    rows = (
        await session.scalars(
            select(CbUpdatePost).order_by(CbUpdatePost.published_at.desc()).limit(50)
        )
    ).all()
    items = [
        UpdatePostOut(
            id=row.id,
            title=row.title,
            body_md=row.body_md,
            published_at=row.published_at,
            source=row.source,
            basket_id=row.basket_id,
            manager_id=row.manager_id,
        )
        for row in rows
    ]
    return UpdatePostListOut(items=items, count=len(items))


async def _load_open_action(
    session: SessionDep,
    *,
    user_id: int,
    action_id: int,
) -> CbPendingAction:
    action = await session.get(CbPendingAction, action_id)
    if action is None or action.user_id != user_id:
        raise not_found("pending action", str(action_id))
    if action.dismissed_at is not None or action.resolved_at is not None:
        raise not_found("pending action", str(action_id))
    return action


@router.post(
    "/cb/pending-actions/{action_id}/dismiss",
    response_model=PendingActionMutationOut,
)
async def dismiss_pending_action(
    action_id: int,
    session: SessionDep,
    principal: AuthenticatedDep,
) -> PendingActionMutationOut:
    user_id = await scoped_sole_user_id(session, principal.user_id)
    action = await _load_open_action(session, user_id=user_id, action_id=action_id)
    action.dismissed_at = dt.datetime.now(tz=dt.UTC)
    await session.commit()
    await session.refresh(action)
    return PendingActionMutationOut(
        id=action.id,
        type=action.type,
        dismissed_at=action.dismissed_at,
        resolved_at=action.resolved_at,
    )


@router.post(
    "/cb/pending-actions/{action_id}/resolve",
    response_model=PendingActionMutationOut,
)
async def resolve_pending_action(
    action_id: int,
    session: SessionDep,
    principal: AuthenticatedDep,
) -> PendingActionMutationOut:
    user_id = await scoped_sole_user_id(session, principal.user_id)
    action = await _load_open_action(session, user_id=user_id, action_id=action_id)
    action.resolved_at = dt.datetime.now(tz=dt.UTC)
    await session.commit()
    await session.refresh(action)
    return PendingActionMutationOut(
        id=action.id,
        type=action.type,
        dismissed_at=action.dismissed_at,
        resolved_at=action.resolved_at,
    )
