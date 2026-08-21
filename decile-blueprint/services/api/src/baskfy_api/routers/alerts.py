"""``/alerts`` — screen-alert subscriptions and one-click unsubscribe (Prompt 20 deliverable 3).

Not in docs/07. Same reasoning as ``/keys`` and ``/admin``: the web app calls it, so it is typed
end to end (docs/02 rule 5) and tagged so a reader of the spec can see which part of the surface
is not the document's.

``POST /alerts/unsubscribe`` is the one route here that takes **no session**. A one-click
unsubscribe link in an email is followed by a mail client, a link scanner or a person who is not
signed in; requiring authentication would make the link useless to exactly the recipients most
likely to use it. The token is 128 unguessable bits derived per alert
(``baskfy_api.alerts.unsubscribe_token``), it grants nothing except turning that one alert off,
and the response is the same whether the token matched or not — an unsubscribe endpoint that
distinguished them would be a way to test tokens.
"""

from __future__ import annotations

from fastapi import APIRouter, Path, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api import alerts as service
from baskfy_api.auth import AuthenticatedDep, Principal, settings_for
from baskfy_api.db import SessionDep
from baskfy_api.problems import Problem, ProblemType, not_found
from baskfy_api.schemas import (
    ScreenAlertCreate,
    ScreenAlertDeliveryListOut,
    ScreenAlertDeliveryOut,
    ScreenAlertListOut,
    ScreenAlertOut,
    ScreenAlertUpdate,
    UnsubscribeIn,
    UnsubscribeOut,
)
from baskfy_core.models import Screen, ScreenAlert, ScreenAlertDelivery

router = APIRouter(prefix="/alerts", tags=["alerts"])

#: How many past nights the delivery history returns. Enough to see a week of silence and ask why.
HISTORY_LIMIT = 30


def _bad_request(detail: str) -> Problem:
    """docs/07's only 400. See ``routers/api_keys._bad_request`` for why it is reused."""
    return Problem(ProblemType.INVALID_SCREEN_DEFINITION, detail, errors=[{"message": detail}])


def _out(alert: ScreenAlert, screen: Screen) -> ScreenAlertOut:
    frequency = alert.frequency
    if frequency not in {"daily", "weekly"}:  # pragma: no cover - the check constraint forbids it
        raise Problem(ProblemType.INTERNAL_ERROR, f"alert {alert.public_id} has a bad frequency")
    return ScreenAlertOut(
        public_id=alert.public_id,
        screen_public_id=screen.public_id,
        screen_name=screen.name,
        frequency="daily" if frequency == "daily" else "weekly",
        weekday=alert.weekday,
        top_n=alert.top_n,
        min_move=alert.min_move,
        digest=alert.digest,
        is_active=alert.is_active,
        last_sent_at=alert.last_sent_at,
    )


async def _screen_for(session: AsyncSession, alert: ScreenAlert) -> Screen:
    screen = (
        await session.execute(select(Screen).where(Screen.id == alert.screen_id))
    ).scalar_one_or_none()
    if screen is None:  # pragma: no cover - the FK cascades, so this cannot normally happen
        raise Problem(ProblemType.INTERNAL_ERROR, "alert points at a screen that is gone")
    return screen


async def _load(session: AsyncSession, principal: Principal, public_id: str) -> ScreenAlert:
    alert = await service.load_alert(session, principal.require_user(), public_id)
    if alert is None:
        raise not_found("alert", public_id)
    return alert


@router.get("", response_model=ScreenAlertListOut, summary="List screen alerts")
async def list_screen_alerts(
    session: SessionDep, principal: AuthenticatedDep
) -> ScreenAlertListOut:
    alerts = await service.list_alerts(session, principal.require_user())
    out: list[ScreenAlertOut] = []
    for alert in alerts:
        out.append(_out(alert, await _screen_for(session, alert)))
    return ScreenAlertListOut(alerts=out)


@router.post(
    "",
    response_model=ScreenAlertOut,
    status_code=status.HTTP_201_CREATED,
    summary="Subscribe a screen to alerts",
)
async def create_screen_alert(
    body: ScreenAlertCreate, session: SessionDep, principal: AuthenticatedDep, request: Request
) -> ScreenAlertOut:
    """Subscribe. A screen the caller cannot see is a 404, not a 403 — see ``require_staff``."""
    user_id = principal.require_user()
    screen = (
        await session.execute(select(Screen).where(Screen.public_id == body.screen_public_id))
    ).scalar_one_or_none()
    if screen is None or (screen.user_id is not None and screen.user_id != user_id):
        raise not_found("screen", body.screen_public_id)

    existing = (
        await session.execute(
            select(ScreenAlert).where(
                ScreenAlert.user_id == user_id, ScreenAlert.screen_id == screen.id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise _bad_request(
            "This screen already has an alert on this account; update it instead of adding one."
        )
    try:
        alert = await service.create_alert(
            session,
            user_id=user_id,
            screen=screen,
            settings=settings_for(request),
            frequency=body.frequency,
            weekday=body.weekday,
            top_n=body.top_n,
            min_move=body.min_move,
            digest_preference=body.digest,
        )
    except ValueError as exc:
        raise _bad_request(str(exc)) from exc
    return _out(alert, screen)


@router.patch("/{public_id}", response_model=ScreenAlertOut, summary="Update a screen alert")
async def update_screen_alert(
    body: ScreenAlertUpdate,
    session: SessionDep,
    principal: AuthenticatedDep,
    public_id: str = Path(...),
) -> ScreenAlertOut:
    alert = await _load(session, principal, public_id)
    try:
        await service.update_alert(
            session,
            alert,
            frequency=body.frequency,
            weekday=body.weekday,
            top_n=body.top_n,
            min_move=body.min_move,
            digest_preference=body.digest,
            is_active=body.is_active,
        )
    except ValueError as exc:
        raise _bad_request(str(exc)) from exc
    return _out(alert, await _screen_for(session, alert))


@router.delete(
    "/{public_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a screen alert"
)
async def delete_screen_alert(
    session: SessionDep, principal: AuthenticatedDep, public_id: str = Path(...)
) -> Response:
    alert = await _load(session, principal, public_id)
    await service.delete_alert(session, alert)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{public_id}/deliveries",
    response_model=ScreenAlertDeliveryListOut,
    summary="What this alert did, night by night",
)
async def alert_deliveries(
    session: SessionDep, principal: AuthenticatedDep, public_id: str = Path(...)
) -> ScreenAlertDeliveryListOut:
    """Including the skips, with their reasons — "no email arrived" needs an answer."""
    alert = await _load(session, principal, public_id)
    rows = (
        (
            await session.execute(
                select(ScreenAlertDelivery)
                .where(ScreenAlertDelivery.alert_id == alert.id)
                .order_by(ScreenAlertDelivery.as_of.desc())
                .limit(HISTORY_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    return ScreenAlertDeliveryListOut(
        deliveries=[
            ScreenAlertDeliveryOut(
                as_of=row.as_of,
                previous_as_of=row.previous_as_of,
                status="sent"
                if row.status == "sent"
                else ("failed" if row.status == "failed" else "skipped"),
                entry_count=row.entry_count,
                exit_count=row.exit_count,
                change_count=row.change_count,
                detail=row.detail,
            )
            for row in rows
        ]
    )


@router.post(
    "/unsubscribe",
    response_model=UnsubscribeOut,
    summary="One-click unsubscribe from an alert email",
)
async def unsubscribe_from_alert(body: UnsubscribeIn, session: SessionDep) -> UnsubscribeOut:
    """No authentication, by design — see the module docstring. Idempotent, and never an oracle."""
    alert = await service.unsubscribe(session, body.token)
    if alert is None:
        return UnsubscribeOut(unsubscribed=True, screen_name=None)
    screen = await _screen_for(session, alert)
    return UnsubscribeOut(unsubscribed=True, screen_name=screen.name)
