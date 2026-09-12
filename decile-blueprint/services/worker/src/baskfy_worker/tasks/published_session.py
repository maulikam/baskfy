"""One answer to "what is the last published session", for every sleeve that asks.

`gates/sleeve-read-contract.md` **C3**. Before this module there were two answers. Swing's
"Scan now" read ``max(ohlcv_daily.date)`` — *the newest date with a bar* — while VBT's and TWT's
read ``max(pipeline_run.trade_date) WHERE data_version IS NOT NULL`` — *the newest date the chain
published*. They agree on an ordinary evening and come apart on exactly the day it matters: the
bars land first and the data quality gate refuses the day afterwards.

That is not hypothetical. `pipeline_run` 48 on the box, trade_date 2026-09-11, records
``fetch_daily_bars succeeded rows_out=4358`` followed by ``data_quality_gate failed``, and
``data_version`` stayed NULL until the 22:49 re-run. For those four hours a swing scan would have
re-detected a session the gate had refused — on the partial, unvalidated bars that caused the
refusal — while VBT's and TWT's correctly answered 2026-09-10.

**The published-run rule wins, and the reason is a house rule, not a preference.** A day the
quality gate refused is not a published day (root `CLAUDE.md`, "which date the product shows":
the product serves the last *completed, published* session, and M88's reverted 15:45 cutoff is
what serving a partial day costs). Bars exist before a run is published; a `data_version` is the
product's own statement that the day is on the page. `baskfy_api.swing_health.published_trade_date`
already asks it this way, and so does the freshness pill.

Ordered by ``data_version`` rather than ``trade_date`` deliberately: the version is the publish
order, and a late re-publish of an older day should not make that older day "the latest".
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import PipelineRun

__all__ = ["last_published_session"]


async def last_published_session(
    session: AsyncSession, on_or_before: dt.date | None = None
) -> dt.date | None:
    """The trade date of the most recently **published** run, or None before the first.

    ``on_or_before`` bounds the answer for a caller that is reconstructing a particular day;
    omitted, it is "the newest published session, full stop", which is what a button pressed now
    means.
    """
    query = select(PipelineRun.trade_date).where(PipelineRun.data_version.is_not(None))
    if on_or_before is not None:
        query = query.where(PipelineRun.trade_date <= on_or_before)
    return (
        await session.execute(query.order_by(PipelineRun.data_version.desc()).limit(1))
    ).scalar_one_or_none()
