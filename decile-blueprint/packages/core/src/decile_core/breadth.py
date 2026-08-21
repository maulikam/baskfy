"""Market-health breadth — the arithmetic behind docs/01 §6's four gauges.

Pure, in the same sense ``decile_core.screener`` is pure: it builds a SQLAlchemy ``Select`` and
touches nothing. The nightly step (``decile_worker.tasks.market_health``) executes it and stores
the result; the seed CLI executes it to populate a development database. One implementation, so a
gauge on the dashboard and a gauge in a test cannot disagree about what "above the 200-day moving
average" counts.

docs/01 §6, verbatim:

    "Four breadth gauges: **Above 200 DMA 57.7%**, **Above 50 DMA 49.2%**, **Within 10% of ATH
     17.6%**, **1Y Return > 0% 45.1%**."

    "Because history starts 1 Nov 2024, these are stored as a daily snapshot table, not
     recomputed."
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Final

from sqlalchemy import ColumnElement, Numeric, Select, case, cast, func, select
from sqlalchemy.orm import InstrumentedAttribute

from decile_core.models import FactorDaily, IndexMemberDaily

#: docs/01 §6 — "Within 10% of ATH".
ATH_PROXIMITY_PCT: Final[Decimal] = Decimal("10")

#: The four ``market_health_daily`` columns, in the order docs/01 §6 renders them.
BREADTH_COLUMNS: Final[tuple[str, ...]] = (
    "pct_above_200dma",
    "pct_above_50dma",
    "pct_within_10pct_ath",
    "pct_ret_1y_positive",
)


def breadth_query(
    index_id: int, on: dt.date
) -> Select[tuple[int, Decimal, Decimal, Decimal, Decimal]]:
    """``(constituent_count, above_200dma, above_50dma, within_10pct_ath, ret_1y_positive)``.

    Point-in-time throughout: the constituents are ``index_member_daily`` rows *for that date*, and
    the factor row is joined on the same date. CLAUDE.md house rule 5 — a breadth series computed
    against today's membership would rewrite history every night.
    """
    return (
        select(
            func.count().label("constituents"),
            _pct(FactorDaily.close > FactorDaily.ma_200, FactorDaily.ma_200),
            _pct(FactorDaily.close > FactorDaily.ma_50, FactorDaily.ma_50),
            _pct(
                func.abs(FactorDaily.away_high_ath) <= ATH_PROXIMITY_PCT,
                FactorDaily.away_high_ath,
            ),
            _pct(FactorDaily.ret_12m > 0, FactorDaily.ret_12m),
        )
        .select_from(IndexMemberDaily)
        .join(
            FactorDaily,
            (FactorDaily.instrument_id == IndexMemberDaily.instrument_id)
            & (FactorDaily.date == IndexMemberDaily.date),
        )
        .where(IndexMemberDaily.index_id == index_id, IndexMemberDaily.date == on)
    )


def _pct(
    predicate: ColumnElement[bool], input_column: InstrumentedAttribute[Decimal | None]
) -> ColumnElement[Decimal]:
    """Percentage of constituents satisfying ``predicate``, or NULL when the input is absent.

    Two details that matter:

    * ``NULLIF(count(input), 0)`` turns "no data" into NULL rather than a division by zero or a
      misleading 0% — "0% of the market is above its 200-day average" is a claim about the market,
      not about our pipeline.
    * the ratio is cast to ``numeric`` before rounding. PostgreSQL has ``round(numeric, int)`` but
      no ``round(double precision, int)``, and ``market_health_daily`` stores numeric(7,4)
      anyway (docs/04) — money and percentages never travel as floats.
    """
    numerator = func.count(case((predicate, 1)))
    denominator = func.nullif(func.count(input_column), 0)
    ratio = cast(numerator, Numeric(20, 10)) * 100 / cast(denominator, Numeric(20, 10))
    return func.round(ratio, 4)
