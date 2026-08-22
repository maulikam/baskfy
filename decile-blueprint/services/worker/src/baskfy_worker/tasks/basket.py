"""Step 11 — ``refresh_basket``: compute the basket once a night instead of once a request (M30).

`/baskets` used to build its answer live. Every bar the 271 scanned symbols have ever had, loaded
into Polars, scored, and turned into a plan — on every page view. That cost about a second while
`ohlcv_daily` held two years. M29 took it to nine and the page went to **67 seconds**
(`DECISIONS-MERGE.md` M29.6).

None of that work is per-request work. Its inputs — bars, factors, the published `data_version` —
change exactly once a night, in the chain this step now belongs to. So it runs after ``publish``,
stores the result in ``basket_snapshot``, and the page reads a row.

WHY AFTER ``publish`` AND NOT BEFORE
------------------------------------
`publish` is what bumps `data_version`, and the basket's identity includes it (docs/06's cache
key is definition + as-of + data version). Computed before, the snapshot would carry the *previous*
version and the page would serve a basket labelled with a data set it was not built from.

It is also the last step for a blunter reason: this is a **presentation cache**. A failure here
must not fail the run or hold back a `data_version` that is otherwise good, so the step records
its own failure and the chain continues — the opposite of the quality gate two steps earlier.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from typing import Final

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import BasketSnapshot
from baskfy_worker.steps import StepOutcome

#: Kept out of the module import graph: `baskfy_api` is the API service, and the worker importing
#: it at module scope would make the two packages circular at startup. The nightly chain is the
#: only caller and it can afford one deferred import.
_BUILDER: Final = "baskfy_api.baskets"


async def run_refresh_basket(
    session: AsyncSession, outcome: StepOutcome, as_of: dt.date | None = None
) -> int:
    """Compute the current basket and store it. Returns the number of names in it.

    Idempotent: the same date and data version overwrite their own row rather than adding one
    (house rule 7). Re-running a night changes nothing except `computed_at`.
    """
    from baskfy_api.baskets import build_current_basket  # noqa: PLC0415 - see _BUILDER

    started = time.perf_counter()
    try:
        basket = await build_current_basket(session, as_of)
    except Exception as exc:
        outcome.note(
            skipped_reason=f"{type(exc).__name__}: {exc}",
            hint="the basket needs bars and one uploaded scan; neither is this step's to create",
        )
        return 0

    # `build_current_basket` RETURNS None when there is nothing to build -- it does not raise, so
    # the except above never sees it. Without this the step would fall through to
    # `None.model_dump_json()` and take the whole nightly run down on any day without an uploaded
    # scan, which is the exact failure the step is written to avoid.
    if basket is None:
        outcome.note(
            skipped_reason="no basket could be built",
            hint="needs bars in the pipeline and one uploaded scan for marketcap, beta and F&O",
        )
        return 0

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    payload = json.loads(basket.model_dump_json())
    # `BasketOut.as_of` is a `str` -- the response model serialises it for the client -- and the
    # column is a DATE. asyncpg will not coerce one to the other, and the failure is a runtime
    # DataError rather than anything a type checker would have caught at the call site.
    as_of_date = dt.date.fromisoformat(basket.as_of)

    statement = insert(BasketSnapshot).values(
        as_of=as_of_date,
        screen_run_id=basket.screen_run_id,
        data_version=basket.data_version,
        payload=payload,
        computed_ms=elapsed_ms,
        computed_at=dt.datetime.now(tz=dt.UTC),
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[BasketSnapshot.as_of, BasketSnapshot.data_version],
            set_={
                "screen_run_id": statement.excluded.screen_run_id,
                "payload": statement.excluded.payload,
                "computed_ms": statement.excluded.computed_ms,
                "computed_at": statement.excluded.computed_at,
            },
        )
    )

    outcome.rows_out = len(basket.rows)
    outcome.note(
        as_of=str(as_of_date),
        data_version=basket.data_version,
        names=len(basket.rows),
        suspect_symbols=len(basket.suspect_symbols),
        computed_ms=elapsed_ms,
    )
    return len(basket.rows)


async def latest_snapshot(
    session: AsyncSession, as_of: dt.date | None = None
) -> dict[str, object] | None:
    """The most recent stored basket, or None when the chain has not written one yet.

    None is a real answer and the caller must handle it: on a fresh database, or on any day the
    step failed, there is no snapshot and the endpoint falls back to computing live. A page that
    404s because a cache is cold would be worse than a slow page.
    """
    query = "select payload from basket_snapshot"
    params: dict[str, object] = {}
    if as_of is not None:
        query += " where as_of = :as_of"
        params["as_of"] = as_of
    query += " order by as_of desc, data_version desc limit 1"
    row = (await session.execute(text(query), params)).first()
    if row is None:
        return None
    payload = row[0]
    return json.loads(payload) if isinstance(payload, str) else dict(payload)
