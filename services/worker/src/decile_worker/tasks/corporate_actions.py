"""Step 3 — ``fetch_corporate_actions`` (docs/03: "NSE; upsert into corporate_action").

The unique key is ``(instrument_id, action_type, ex_date)`` (docs/04), so re-running the step for
an overlapping window converges rather than duplicating.

This step also answers the question step 4 needs: *which instruments have an action we have not
adjusted for yet?* It returns the set of instrument ids it touched, and the orchestrator hands
that to ``apply_adjustments`` — docs/09 §Schedule is explicit that adjustments run for "only
instruments with new actions", not for the whole universe every night.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import cast

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from decile_core.models import CorporateAction, Instrument
from decile_providers.errors import ProviderError
from decile_providers.records import CorporateAction as CorporateActionRecord
from decile_worker.steps import StepOutcome


async def run_fetch_corporate_actions(
    session: AsyncSession, provider: object, outcome: StepOutcome, since: dt.date
) -> set[int]:
    """Upsert corporate actions with an ex-date on or after ``since``.

    Returns the instrument ids whose action set changed, so step 4 knows what to reprocess.
    """
    fetch = getattr(provider, "corporate_actions", None)
    if not callable(fetch):
        outcome.note(reason="no provider offers corporate_actions")
        return set()

    try:
        records: Sequence[CorporateActionRecord] = fetch(since)
    except ProviderError as exc:
        outcome.note(error=str(exc))
        raise

    outcome.rows_in = len(records)
    if not records:
        outcome.note(since=since.isoformat(), touched=0)
        return set()

    ids = dict((await session.execute(select(Instrument.symbol, Instrument.id))).tuples().all())
    unknown = sorted({r.symbol for r in records if r.symbol not in ids})

    values = [
        {
            "instrument_id": ids[record.symbol],
            "action_type": record.action_type,
            "ex_date": record.ex_date,
            "ratio_from": record.ratio_from,
            "ratio_to": record.ratio_to,
            "amount": record.amount,
            "raw": record.raw,
        }
        for record in records
        if record.symbol in ids
    ]
    if not values:
        outcome.note(since=since.isoformat(), unknown_symbols=unknown or None, touched=0)
        return set()

    # NSE really does publish two distinct actions sharing this table's key. Observed on the first
    # real backfill: SIYSIL announced "Scheme Of Arrangement - Bonus Ncrps 3:1" *and* "... 4:1" on
    # the same ex-date, so `(instrument_id, 'bonus', 2026-08-21)` arrives twice in one file.
    #
    # docs/04 keys `corporate_action` on `(instrument_id, action_type, ex_date)`, which cannot
    # represent that, and PostgreSQL refuses a single ON CONFLICT DO UPDATE whose VALUES contain
    # the same key twice ("cannot affect row a second time") — so before this, one such
    # announcement failed the *whole* fetch and no action for any instrument was written.
    #
    # Collapsing to the last occurrence is what sequential upserts would have produced. It is a
    # lossy answer to a schema question (docs/DECISIONS.md §21.5) and it is therefore reported,
    # never silent: `duplicate_keys` names every collision so a missed adjustment is visible.
    last_index: dict[tuple[int, str, dt.date], int] = {}
    collisions: list[str] = []
    for index, value in enumerate(values):
        key = (
            int(str(value["instrument_id"])),
            str(value["action_type"]),
            cast(dt.date, value["ex_date"]),
        )
        if key in last_index:
            collisions.append(f"{key[0]}:{key[1]}:{key[2].isoformat()}")
        last_index[key] = index
    values = [values[index] for index in sorted(last_index.values())]

    stmt = insert(CorporateAction).values(values)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[
                CorporateAction.instrument_id,
                CorporateAction.action_type,
                CorporateAction.ex_date,
            ],
            set_={
                "ratio_from": stmt.excluded.ratio_from,
                "ratio_to": stmt.excluded.ratio_to,
                "amount": stmt.excluded.amount,
                "raw": stmt.excluded.raw,
            },
        )
    )

    touched = {ids[record.symbol] for record in records if record.symbol in ids}
    outcome.rows_out = len(values)
    outcome.note(
        since=since.isoformat(),
        touched=len(touched),
        # Symbols NSE announces an action for that we have never listed. Usually a rename; worth
        # seeing, because a missed rename means a missed adjustment.
        unknown_symbols=unknown or None,
        # Two announcements this table's key cannot tell apart; only one survived.
        duplicate_keys=sorted(set(collisions)) or None,
    )
    return touched
