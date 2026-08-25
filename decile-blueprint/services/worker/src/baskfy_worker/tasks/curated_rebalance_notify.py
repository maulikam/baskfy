"""Email once per undismissed REBALANCE_AVAILABLE pending action (T8.3).

Stamps ``payload.notified_at`` after a successful send so a second Beat run is a no-op.
Does not edit the publish path. Does not place an order.
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from baskfy_api.email.sender import Mailer, build_transport
from baskfy_api.email.templates import rebalance_available
from baskfy_api.settings import get_settings
from baskfy_core.models import AppUser, CbBasket, CbPendingAction
from baskfy_core.models.base import JsonObject

log = logging.getLogger(__name__)

UTC = dt.UTC
_ACTION_TYPE = "REBALANCE_AVAILABLE"


async def run_curated_rebalance_notify(
    session: AsyncSession,
    *,
    mailer: Mailer | None = None,
    now: dt.datetime | None = None,
) -> JsonObject:
    """Send at most one email per open ``REBALANCE_AVAILABLE`` row lacking ``notified_at``."""
    moment = now or dt.datetime.now(tz=UTC)
    postman = mailer or Mailer(build_transport(get_settings()))

    rows = list(
        (
            await session.scalars(
                select(CbPendingAction).where(
                    CbPendingAction.type == _ACTION_TYPE,
                    CbPendingAction.dismissed_at.is_(None),
                    CbPendingAction.resolved_at.is_(None),
                )
            )
        ).all()
    )
    sent = 0
    skipped = 0
    for action in rows:
        payload = dict(action.payload) if isinstance(action.payload, dict) else {}
        if payload.get("notified_at"):
            skipped += 1
            continue
        user = await session.get(AppUser, int(action.user_id))
        if user is None or not str(user.email).strip():
            skipped += 1
            continue
        basket_name = "your basket"
        raw_basket = payload.get("basket_id")
        if isinstance(raw_basket, int):
            basket = await session.get(CbBasket, raw_basket)
            if basket is not None:
                basket_name = str(basket.name)
        version_raw = payload.get("version_no")
        version_no = version_raw if isinstance(version_raw, int) else None
        message = rebalance_available(
            str(user.email),
            basket_name=basket_name,
            version_no=version_no,
        )
        delivered = await postman.deliver(message)
        if not delivered:
            log.warning("rebalance notify failed for action %s", action.id)
            skipped += 1
            continue
        payload["notified_at"] = moment.isoformat()
        payload["delivery"] = "email"
        action.payload = payload
        if sa_inspect(action, raiseerr=False) is not None:
            flag_modified(action, "payload")
        sent += 1
    await session.flush()
    return {"examined": len(rows), "sent": sent, "skipped": skipped}
