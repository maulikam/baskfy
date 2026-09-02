"""The catalyst feed's read model — a link per name, for the setups and watch rows (SW11B).

`docs/swing/STANDING-ANSWERS.md` A3: "link out, do not reproduce text". What the pages get
per instrument is the newest announcement's **headline, stamp and URL** plus the **earnings
date** the calendar row carries — four fields, from `sw_catalyst`, in one query for the whole
page. Nothing here reads a filing; nothing here has a route of its own (the two GETs that
already exist carry it — `test_swing_readonly.py` stays exactly as strict).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import SwCatalyst

#: `SwCatalyst.source` values, restated here so the read model does not import the provider.
ANNOUNCEMENT = "NSE_ANNOUNCEMENT"
EVENT_CALENDAR = "NSE_EVENT_CALENDAR"


@dataclass(frozen=True, slots=True)
class CatalystView:
    """What a row on `/swing` links out to. Any of the four may be absent; the row is never."""

    headline: str | None
    published_at: dt.datetime | None
    url: str | None
    earnings_date: dt.date | None

    @property
    def empty(self) -> bool:
        return self.url is None and self.earnings_date is None


async def latest_for(
    session: AsyncSession, *, user_id: int, instrument_ids: Sequence[int]
) -> dict[int, CatalystView]:
    """The newest announcement and the earnings date per instrument, in one query.

    Newest by ``published_at``; an undated announcement is a link but never "newest" unless it
    is the only one. The earnings date comes from the calendar row, which the feed keeps at
    the nearest result meeting on or after its last run and deletes when there is none.
    """
    wanted = sorted({int(instrument_id) for instrument_id in instrument_ids})
    if not wanted:
        return {}
    rows = (
        await session.execute(
            select(SwCatalyst)
            .where(SwCatalyst.user_id == user_id, SwCatalyst.instrument_id.in_(wanted))
            .order_by(
                SwCatalyst.instrument_id,
                SwCatalyst.published_at.desc().nulls_last(),
                SwCatalyst.id.desc(),
            )
        )
    ).scalars()
    newest: dict[int, SwCatalyst] = {}
    earnings: dict[int, dt.date] = {}
    for row in rows:
        key = int(row.instrument_id)
        if row.source == EVENT_CALENDAR:
            if row.earnings_date is not None and key not in earnings:
                earnings[key] = row.earnings_date
            continue
        newest.setdefault(key, row)
    views: dict[int, CatalystView] = {}
    for key in set(newest) | set(earnings):
        top = newest.get(key)
        views[key] = CatalystView(
            headline=top.headline if top is not None else None,
            published_at=top.published_at if top is not None else None,
            url=top.url if top is not None else None,
            earnings_date=earnings.get(key),
        )
    return views
