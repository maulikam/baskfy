"""``/managers/*`` — a manager stops being a seed row and becomes a person who can join.

    GET    /managers/me                                 the caller's manager identity
    POST   /managers/me                                 apply, or resubmit a rejected application
    PATCH  /managers/{slug}                             staff review: approve / reject / suspend
    POST   /managers/me/baskets/{basket_slug}/publish   publish one of your own baskets
    DELETE /managers/me/baskets/{basket_slug}/publish   take it down again
    GET    /managers/me/revenue-share                   dark while fee collection is off

WHAT WAS MISSING
----------------
``cb_manager`` held exactly two rows, both written by ``curated_seed``: the engine and the
operator. There was no column joining a manager to an account, no application, no review, and
nothing deciding who may flip ``cb_basket.visibility``. A manager was a row, not a person.

Migration 0020 supplies the columns. This router supplies the verbs, and it deliberately supplies
them in the narrow order the working agreement allows:

**Applying is not public signup.** ``POST /managers/me`` requires an authenticated account, so it
opens nothing that ``BASKFY_PUBLIC_SIGNUP_ENABLED`` gates — an existing user declares they want to
manage; a stranger still cannot create an account. That flag stays false and this router does not
read it.

**Review is staff-only, and the transitions are not this module's opinion.** Every legal move is
:func:`baskfy_core.manager_onboarding.next_state`, so the API and the state machine's own tests
cannot disagree. In particular a suspended manager cannot be restored to APPROVED in one step;
the path back runs through SUBMITTED so that a second decision is actually made.

**Publishing is a listing, never an order.** Setting ``visibility`` to PUBLISHED makes a basket
findable. It places nothing, and this module imports nothing from the execution package.

**Revenue share is dark.** ``GET /managers/me/revenue-share`` 404s while
``BASKFY_FEE_COLLECTION_ENABLED`` is false — the pattern ``routers/track_b.py`` already
established — and it reports whatever rate an operator has recorded. It invents no rate: D7
pricing amounts are human-track and the column has no default for exactly that reason.

TENANCY
-------
Every ``/me`` route resolves the manager from the caller's own ``user_id``. A basket that belongs
to somebody else's manager is reported as NOT_FOUND rather than FORBIDDEN, so the surface never
confirms that a slug exists for a caller who has no business knowing.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.auth import AuthenticatedDep, Principal, require_staff
from baskfy_api.db import SessionDep
from baskfy_api.problems import Problem, ProblemType, not_found
from baskfy_api.settings import Settings, get_settings
from baskfy_api.track_b import require_fee_collection_enabled
from baskfy_core.manager_onboarding import ManagerStateError, may_publish, next_state
from baskfy_core.models import CbBasket, CbManager
from baskfy_core.models.curated_baskets import CbManagerRevenueShare
from baskfy_core.sebi_registration import REGISTRATION_TYPES, check_format

router = APIRouter(tags=["managers"])

SettingsDep = Annotated[Settings, Depends(get_settings)]
StaffDep = Annotated[Principal, Depends(require_staff)]

RegistrationType = Literal[
    "NONE",
    "RESEARCH_ANALYST",
    "INVESTMENT_ADVISER",
    "PORTFOLIO_MANAGER",
    "AIF",
    "MF_DISTRIBUTOR",
    "UNKNOWN",
]


class ManagerOut(BaseModel):
    """A manager identity as its holder sees it."""

    slug: str
    name: str
    kind: str
    state: str
    may_publish: bool
    sebi_reg_type: str
    sebi_reg_no: str | None
    sebi_reg_valid_from: dt.date | None
    sebi_reg_valid_to: dt.date | None
    #: NULL until an operator has compared the number against the SEBI register. A well-formed
    #: number is not a verified one, and neither is a statement about compliance.
    sebi_reg_verified_at: dt.datetime | None
    #: Always present, always the same sentence. The registration fields are captured data, not
    #: an assertion by Baskfy that the holder may manage anyone's money.
    registration_disclaimer: str


class ManagerApplyIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    bio: str | None = Field(default=None, max_length=4000)
    sebi_reg_type: RegistrationType = "NONE"
    sebi_reg_no: str | None = Field(default=None, max_length=64)
    sebi_reg_valid_from: dt.date | None = None
    sebi_reg_valid_to: dt.date | None = None


class ManagerReviewIn(BaseModel):
    """A staff decision. ``state`` is the state being moved to, not a verb."""

    state: Literal["APPROVED", "REJECTED", "SUSPENDED", "SUBMITTED"]
    note: str | None = Field(default=None, max_length=2000)


class RevenueShareOut(BaseModel):
    manager_slug: str
    rate_bps: int | None
    effective_from: dt.date | None
    effective_to: dt.date | None
    note: str | None
    #: True when no agreement row exists. The absence of a rate is reported as absence, never as
    #: a zero — those mean different things and only one of them is a decision.
    unset: bool


_DISCLAIMER = (
    "Registration details are captured as supplied and are not verified by Baskfy unless a "
    "verification date is shown. A registration is not a statement by Baskfy that this manager "
    "may manage your money."
)


def _out(manager: CbManager) -> ManagerOut:
    return ManagerOut(
        slug=manager.slug,
        name=manager.name,
        kind=manager.kind,
        state=manager.state,
        may_publish=may_publish(manager.state),
        sebi_reg_type=manager.sebi_reg_type,
        sebi_reg_no=manager.sebi_reg_no,
        sebi_reg_valid_from=manager.sebi_reg_valid_from,
        sebi_reg_valid_to=manager.sebi_reg_valid_to,
        sebi_reg_verified_at=manager.sebi_reg_verified_at,
        registration_disclaimer=_DISCLAIMER,
    )


async def _mine(session: AsyncSession, user_id: int) -> CbManager:
    """The caller's manager identity, or 404. Never another account's."""
    found = (
        await session.execute(select(CbManager).where(CbManager.user_id == user_id))
    ).scalar_one_or_none()
    if found is None:
        raise not_found("manager", "me")
    return found


def _validated_registration(body: ManagerApplyIn) -> tuple[str, str | None]:
    """Run the pure format check and turn a refusal into a 400 the caller can act on.

    A number in a shape nothing recognises is stored as ``UNKNOWN`` rather than refused: a manager
    holding a registration we have not seen must still be able to apply, and a person can look at
    it. What is refused is incoherence — a type of NONE carrying a number, or a number missing for
    a type that requires one — because the database refuses those too and a sentence beats an
    integrity error.
    """
    try:
        verdict = check_format(body.sebi_reg_type, body.sebi_reg_no)
    except ValueError as exc:
        raise Problem(ProblemType.INVALID_SCREEN_DEFINITION, str(exc)) from exc
    if body.sebi_reg_type == "NONE":
        return "NONE", None
    return verdict.registration_type, verdict.normalised


@router.get("/managers/me", response_model=ManagerOut, summary="Your manager identity")
async def my_manager(session: SessionDep, principal: AuthenticatedDep) -> ManagerOut:
    return _out(await _mine(session, principal.require_user()))


@router.post(
    "/managers/me",
    response_model=ManagerOut,
    status_code=201,
    summary="Apply to manage baskets, or resubmit after a rejection",
)
async def apply(
    body: ManagerApplyIn, session: SessionDep, principal: AuthenticatedDep
) -> ManagerOut:
    """Create a manager identity in SUBMITTED, or resubmit a rejected one.

    Requires an authenticated account, so this is not public signup and does not read that flag.
    """
    user_id = principal.require_user()
    reg_type, reg_no = _validated_registration(body)
    existing = (
        await session.execute(select(CbManager).where(CbManager.user_id == user_id))
    ).scalar_one_or_none()

    if existing is not None:
        # Editing an application that is still SUBMITTED is not a transition — nobody has
        # decided anything yet, so there is no decision to re-make and `next_state` would
        # (correctly) refuse SUBMITTED -> SUBMITTED as a no-op write. DRAFT and REJECTED are
        # real moves. APPROVED and SUSPENDED are refused: changing the registration behind an
        # approval that was granted against the old one is exactly what review exists for.
        if existing.state != "SUBMITTED":
            try:
                existing.state = next_state(existing.state, "SUBMITTED")
            except ManagerStateError as exc:
                raise Problem(ProblemType.INVALID_SCREEN_DEFINITION, str(exc)) from exc
        existing.name = body.name
        existing.bio = body.bio
        existing.sebi_reg_type = reg_type
        existing.sebi_reg_no = reg_no
        existing.sebi_reg_valid_from = body.sebi_reg_valid_from
        existing.sebi_reg_valid_to = body.sebi_reg_valid_to
        # A resubmission is a fresh claim: whatever was verified before was verified about the
        # previous submission, so the stamp does not carry over.
        existing.sebi_reg_verified_at = None
        await session.flush()
        return _out(existing)

    slug = f"m-{user_id}"
    created = CbManager(
        slug=slug,
        name=body.name,
        kind="EXTERNAL",
        user_id=user_id,
        state=next_state("DRAFT", "SUBMITTED"),
        bio=body.bio,
        strategies=[],
        sebi_reg_type=reg_type,
        sebi_reg_no=reg_no,
        sebi_reg_valid_from=body.sebi_reg_valid_from,
        sebi_reg_valid_to=body.sebi_reg_valid_to,
    )
    session.add(created)
    await session.flush()
    return _out(created)


@router.patch(
    "/managers/{slug}",
    response_model=ManagerOut,
    summary="Staff review: approve, reject or suspend a manager",
)
async def review(
    body: ManagerReviewIn,
    session: SessionDep,
    _staff: StaffDep,
    slug: Annotated[str, Path(min_length=1, max_length=120)],
) -> ManagerOut:
    """Move a manager to ``body.state`` if the transition is legal.

    The legality is :func:`next_state`'s answer, not this router's. A suspended manager therefore
    cannot be restored to APPROVED here either — the refusal names SUBMITTED as the way back.
    """
    manager = (
        await session.execute(select(CbManager).where(CbManager.slug == slug))
    ).scalar_one_or_none()
    if manager is None:
        raise not_found("manager", slug)
    try:
        manager.state = next_state(manager.state, body.state)
    except ManagerStateError as exc:
        raise Problem(ProblemType.INVALID_SCREEN_DEFINITION, str(exc)) from exc
    await session.flush()
    return _out(manager)


async def _my_basket(session: AsyncSession, user_id: int, basket_slug: str) -> CbBasket:
    """One of the caller's own baskets, or 404.

    The join through ``cb_manager.user_id`` is what makes another manager's basket indistinguish-
    able from a slug that does not exist.
    """
    found = (
        await session.execute(
            select(CbBasket)
            .join(CbManager, CbManager.id == CbBasket.manager_id)
            .where(CbBasket.slug == basket_slug, CbManager.user_id == user_id)
        )
    ).scalar_one_or_none()
    if found is None:
        raise not_found("basket", basket_slug)
    return found


class PublishOut(BaseModel):
    basket_slug: str
    visibility: str
    #: Stated on every response so a caller is never left to infer it from the absence of a field.
    placed_an_order: Literal[False] = False


@router.post(
    "/managers/me/baskets/{basket_slug}/publish",
    response_model=PublishOut,
    summary="Publish one of your own baskets (listing only — never an order)",
)
async def publish(
    session: SessionDep,
    principal: AuthenticatedDep,
    basket_slug: Annotated[str, Path(min_length=1, max_length=120)],
) -> PublishOut:
    user_id = principal.require_user()
    manager = await _mine(session, user_id)
    if not may_publish(manager.state):
        raise Problem(
            ProblemType.INVALID_SCREEN_DEFINITION,
            f"a manager in {manager.state} may not publish; an approved review is required first",
        )
    basket = await _my_basket(session, user_id, basket_slug)
    basket.visibility = "PUBLISHED"
    await session.flush()
    return PublishOut(basket_slug=basket.slug, visibility=basket.visibility)


@router.delete(
    "/managers/me/baskets/{basket_slug}/publish",
    response_model=PublishOut,
    summary="Take one of your own baskets back down",
)
async def unpublish(
    session: SessionDep,
    principal: AuthenticatedDep,
    basket_slug: Annotated[str, Path(min_length=1, max_length=120)],
) -> PublishOut:
    """Unpublishing is allowed from any state.

    A suspended manager must be able to take their own listing down — refusing that would trap a
    basket in public view precisely when somebody has decided it should not be.
    """
    user_id = principal.require_user()
    await _mine(session, user_id)
    basket = await _my_basket(session, user_id, basket_slug)
    basket.visibility = "PRIVATE"
    await session.flush()
    return PublishOut(basket_slug=basket.slug, visibility=basket.visibility)


@router.get(
    "/managers/me/revenue-share",
    response_model=RevenueShareOut,
    summary="Your revenue share (dark while fee collection is off)",
)
async def revenue_share(
    session: SessionDep, principal: AuthenticatedDep, settings: SettingsDep
) -> RevenueShareOut:
    """404s while ``BASKFY_FEE_COLLECTION_ENABLED`` is false, as ``track_b`` does elsewhere.

    Reports the recorded agreement and nothing more. If no agreement exists the answer is
    ``unset``, never a rate of zero: D7 amounts are human-track, and a zero would be an invented
    decision dressed as data.
    """
    require_fee_collection_enabled(settings)
    user_id = principal.require_user()
    manager = await _mine(session, user_id)
    agreement = (
        await session.execute(
            select(CbManagerRevenueShare)
            .where(
                CbManagerRevenueShare.manager_id == manager.id,
                CbManagerRevenueShare.effective_to.is_(None),
            )
            .order_by(CbManagerRevenueShare.effective_from.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if agreement is None:
        return RevenueShareOut(
            manager_slug=manager.slug,
            rate_bps=None,
            effective_from=None,
            effective_to=None,
            note=None,
            unset=True,
        )
    return RevenueShareOut(
        manager_slug=manager.slug,
        rate_bps=agreement.rate_bps,
        effective_from=agreement.effective_from,
        effective_to=agreement.effective_to,
        note=agreement.note,
        unset=False,
    )


#: Re-exported so a test can assert the vocabulary the API accepts is the vocabulary the pure
#: module defines, rather than a Literal that drifted.
ACCEPTED_REGISTRATION_TYPES = REGISTRATION_TYPES
