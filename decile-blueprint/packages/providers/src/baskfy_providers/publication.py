"""Did NSE publish a bhavcopy for this date? — the one question that separates a holiday from a
failed ingest.

WHY THIS IS A SHARED MODULE AND NOT A PRIVATE HELPER
====================================================
M62 added this probe to `baskfy_worker.orchestrator` as `_bhavcopy_published`, to stop
`reconcile_calendar` inferring a holiday for a day the exchange had traded. Leaf 3.1's resync
detector has to ask the *same* question from the API service — "this weekday is recorded as an
inferred holiday; did NSE actually publish for it?" — and `baskfy-api` cannot import
`baskfy_worker` (the dependency runs worker -> api; see `baskfy_api.queue`).

Two probes would be two answers. So the probe moved down into `baskfy-providers`, which both
services already depend on, and both call this. `baskfy_worker.calendar.PublicationCheck` is
re-exported from here so nothing that imported it from the worker had to change.

THE ANSWER IS DELIBERATELY ASYMMETRIC
=====================================
`False` means "we found no published file", which covers both "NSE published nothing" and "we
could not tell". That asymmetry is the safe direction *for the calendar*: `reconcile_calendar`
only consults this to decide whether to **refuse** to infer a holiday, so a `False` it should not
have given leaves the pre-M62 behaviour rather than inventing a session.

It is the wrong direction for a caller that wants to *act* on a `True`. Such a caller must treat
`False` as "no evidence" rather than as "the exchange was shut" — which is exactly what
`baskfy_api.resync` does: it only ever promotes a day on a `True`, and reports "could not reach
NSE" as an unresolved finding rather than as a clean bill of health.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Awaitable, Callable

from baskfy_providers.errors import ProviderError

__all__ = ["PublicationCheck", "bhavcopy_publication_check"]

#: "Did NSE publish a bhavcopy for this date?" A coroutine so the caller can answer it from the
#: archive, the network, or a cache without the caller knowing which.
PublicationCheck = Callable[[dt.date], Awaitable[bool]]


def bhavcopy_publication_check(provider: object) -> PublicationCheck | None:
    """Wrap a provider's bhavcopy lookup as the "did NSE publish?" question.

    ``None`` when the provider cannot answer, which leaves `reconcile_calendar` on its pre-M62
    behaviour rather than silently treating "cannot check" as "nothing was published" — the
    latter would re-create the exact bug this guards.
    """
    fetch = getattr(provider, "bhavcopy", None)
    if not callable(fetch):
        return None

    async def published(day: dt.date) -> bool:
        def _probe() -> bool:
            try:
                frame = fetch(day)
            except ProviderError:
                # No file, or the archive could not be read. Either way this is not evidence
                # that NSE traded, so inference proceeds as before.
                return False
            height = getattr(frame, "height", None)
            return bool(height) if height is not None else bool(len(frame))

        return await asyncio.to_thread(_probe)

    return published
