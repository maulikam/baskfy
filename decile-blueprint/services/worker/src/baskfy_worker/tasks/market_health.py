"""Step 8 — ``compute_market_health`` (Prompt 4 deliverable 3).

    "for each of the 12 universes, compute pct_above_200dma, pct_above_50dma,
     pct_within_10pct_ath, pct_ret_1y_positive, and constituent_count, from factor_daily. Store
     daily so history is queryable."

docs/01 §6 is the surface: "Four breadth gauges: Above 200 DMA 57.7%, Above 50 DMA 49.2%, Within
10% of ATH 17.6%, 1Y Return > 0% 45.1%", over the twelve universes its selector lists.

WHERE THE ARITHMETIC LIVES
--------------------------
Not here. ``baskfy_core.breadth.breadth_query`` builds the statement, and this step executes and
stores it — the same split as ``baskfy_core.screener`` versus ``baskfy_api.screener``, and for the
same reason: the seed CLI and this task must not be able to disagree about what "above the 200-day
moving average" counts. Prompt 11's first acceptance criterion checks that arithmetic against a
hand-computed fixture, and there is exactly one implementation for it to check.

All four metrics read factor-engine columns (``ma_200``, ``ma_50``, ``away_high_ath``,
``ret_12m``). A metric whose input column is entirely NULL comes back NULL rather than 0% — which
would render on the dashboard as "no stock in the market is above its 200-day average".
``constituent_count`` comes from ``index_member_daily`` and is always computable.

docs/01 §6: "Because history starts 1 Nov 2024, these are stored as a daily snapshot table, not
recomputed." Hence a row per universe per day rather than a view — which is also what makes
history queryable — ``baskfy_api.market_data.market_health_history`` is the reader, and it joins
the universe's own index level alongside for docs/08's overlay.

Note that the 1 Nov 2024 floor is the *reference product's* limitation, not ours. docs/01 §10
lists "Historical ranks only from Nov 2024" as a gap and our answer as "Store point-in-time index
membership + factors from day 1 of backfill", so this table is populated as far back as the
backfill reaches.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.breadth import ATH_PROXIMITY_PCT, BREADTH_COLUMNS, breadth_query
from baskfy_core.models import MarketHealthDaily
from baskfy_core.universes import MARKET_HEALTH_SLUGS, UNIVERSE_BY_SLUG
from baskfy_worker.steps import StepOutcome

#: Re-exported so the pipeline's callers keep one import site for the step and its constants.
__all__ = ["ATH_PROXIMITY_PCT", "BREADTH_COLUMNS", "run_compute_market_health"]


async def run_compute_market_health(
    session: AsyncSession, outcome: StepOutcome, on: dt.date
) -> int:
    """One breadth row per market-health universe (docs/01 §6 lists twelve)."""
    written = 0
    incomplete: list[str] = []

    for slug in MARKET_HEALTH_SLUGS:
        universe = UNIVERSE_BY_SLUG[slug]
        row = (await session.execute(breadth_query(universe.index_id, on))).one()

        constituents, above_200, above_50, near_ath, positive_1y = row
        if constituents and above_200 is None:
            incomplete.append(slug)

        stmt = insert(MarketHealthDaily).values(
            index_id=universe.index_id,
            date=on,
            pct_above_200dma=above_200,
            pct_above_50dma=above_50,
            pct_within_10pct_ath=near_ath,
            pct_ret_1y_positive=positive_1y,
            constituent_count=constituents,
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[MarketHealthDaily.index_id, MarketHealthDaily.date],
                set_={
                    "pct_above_200dma": stmt.excluded.pct_above_200dma,
                    "pct_above_50dma": stmt.excluded.pct_above_50dma,
                    "pct_within_10pct_ath": stmt.excluded.pct_within_10pct_ath,
                    "pct_ret_1y_positive": stmt.excluded.pct_ret_1y_positive,
                    "constituent_count": stmt.excluded.constituent_count,
                },
            )
        )
        written += 1

    outcome.rows_in = len(MARKET_HEALTH_SLUGS)
    outcome.rows_out = written
    outcome.note(
        date=on.isoformat(),
        # Universes whose breadth is NULL because the factor engine has not run. NULL, not zero:
        # "0% above the 200-day average" is a claim about the market, not about our pipeline.
        breadth_unavailable=incomplete or None,
    )
    return written
