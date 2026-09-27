"""``GET /sleeves/state`` — what each of the three books is doing right now, and why (LV4).

Read-only. One request, three rows, no broker call: the answer is derived from stored rows and
the clock (:mod:`baskfy_api.sleeve_state`). The pages poll it beside their Scan buttons every
thirty seconds. Nothing here queues a scan, builds a plan or reaches the execution package.
"""

from __future__ import annotations

from fastapi import APIRouter

from baskfy_api.auth import AuthenticatedDep
from baskfy_api.curated_tenant import scoped_sole_user_id
from baskfy_api.db import SessionDep
from baskfy_api.schemas import SleeveStateOut
from baskfy_api.sleeve_state import sleeve_states

router = APIRouter(prefix="/sleeves", tags=["sleeves"])


@router.get("/state", response_model=list[SleeveStateOut], summary="Per-sleeve state")
async def get_sleeve_states(
    principal: AuthenticatedDep, session: SessionDep
) -> list[SleeveStateOut]:
    """swing, twt and vbt: ``closed``, ``waiting_for_login``, ``scanning``, ``monitoring``,
    ``plan_ready``, ``missed_window``, ``signal_ready``, ``blocked`` or ``idle`` — with the reason,
    what happens next, and what that sleeve's Scan button actually does."""
    user_id = await scoped_sole_user_id(session, principal.user_id)
    return await sleeve_states(session, user_id=user_id)
