"""Equity fundamentals — folded into ``refresh_index_snapshots`` (T9.1).

docs/05 §14: marketcap and P/E come from ``fundamental_daily``, sourced from NSE. Nothing in
the ten-step chain fetched them, so the join in ``compute_factors`` wrote NULL and the landing
page rendered em dashes.

This upserts ``fundamental_daily`` for one as-of date from ``provider.equity_fundamentals``.
It is not a twelfth pipeline step: docs/03's ten plus M30's cache stay the chain. A night
where NSE does not quote is still publishable — NULL is the current (honest) state.

Market cap is issued shares * that day's ``close_raw`` / Rs 1 crore when a bar exists, else the
quote's last price. Display money uses the exchange print (house rule 6).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import FundamentalDaily, Instrument, OhlcvDaily
from baskfy_providers.errors import ProviderError
from baskfy_providers.records import EquityFundamental
from baskfy_worker.steps import StepOutcome

_CRORE: Decimal = Decimal("10000000")


@dataclass(slots=True)
class FundamentalsResult:
    rows_in: int = 0
    rows_written: int = 0
    unmatched: int = 0


async def fundamentals_scope(session: AsyncSession, on: dt.date) -> list[tuple[str, str | None]]:
    """``(symbol, series)`` for every instrument that actually traded on ``on``.

    Scope is the date's own universe, not the whole listing register, and the reason is a
    stopwatch. NSE is fetched at 1 req/s (docs/09), so the 10,481 non-delisted instruments would
    cost the better part of three hours, most of it spent on names the exchange did not quote —
    while the 2,540 with a bar cost about forty minutes and are exactly the population
    ``compute_factors`` will join against. A quote for a name with no bar has no ``close_raw`` to
    be priced on and could not be stored anyway.

    The series comes along because NSE answers the wrong one with ``200`` and an empty body; the
    listings step already stores it, so it is free here and saves a lookup round trip per symbol.
    """
    rows = await session.execute(
        select(Instrument.symbol, Instrument.series)
        .join(OhlcvDaily, OhlcvDaily.instrument_id == Instrument.id)
        .where(OhlcvDaily.date == on)
        .order_by(Instrument.symbol)
    )
    return [(str(row[0]), row[1]) for row in rows.tuples()]


async def run_fetch_fundamentals(
    session: AsyncSession,
    provider: object,
    outcome: StepOutcome,
    on: dt.date,
    scope: Sequence[tuple[str, str | None]],
) -> int:
    fetch = getattr(provider, "equity_fundamentals", None)
    if not callable(fetch):
        outcome.note(reason="no provider offers equity_fundamentals")
        return 0

    symbols = [symbol for symbol, _ in scope]
    series_by_symbol = {symbol: series for symbol, series in scope if series}
    try:
        records = list(fetch(on, symbols, series_by_symbol=series_by_symbol))
    except ProviderError as exc:
        outcome.note(error=str(exc))
        raise

    result = await store_fundamentals(session, on, records)
    outcome.rows_in = result.rows_in
    outcome.rows_out = result.rows_written
    outcome.note(date=on.isoformat(), unmatched=result.unmatched or None)
    return result.rows_written


async def store_fundamentals(
    session: AsyncSession, on: dt.date, records: list[EquityFundamental]
) -> FundamentalsResult:
    result = FundamentalsResult(rows_in=len(records))
    if not records:
        return result

    wanted = {record.symbol.upper() for record in records}
    mapped = {
        str(row[0]).upper(): int(row[1])
        for row in (
            await session.execute(
                select(Instrument.symbol, Instrument.id).where(Instrument.symbol.in_(wanted))
            )
        ).tuples()
    }
    closes = {
        int(row[0]): row[1]
        for row in (
            await session.execute(
                select(OhlcvDaily.instrument_id, OhlcvDaily.close_raw).where(
                    OhlcvDaily.date == on,
                    OhlcvDaily.instrument_id.in_(mapped.values()),
                )
            )
        ).tuples()
    }

    values: list[dict[str, object]] = []
    for record in records:
        instrument_id = mapped.get(record.symbol.upper())
        if instrument_id is None:
            result.unmatched += 1
            continue
        close = closes.get(instrument_id)
        marketcap_cr = _marketcap_cr(record.shares_outstanding, close, record.last_price)
        if marketcap_cr is None:
            marketcap_cr = record.marketcap_cr
        values.append(
            {
                "instrument_id": instrument_id,
                "date": on,
                "marketcap_cr": marketcap_cr,
                "pe": _pe_on(record.pe, close, record.last_price),
                "pb": record.pb,
                "div_yield": record.div_yield,
                "shares_outstanding": record.shares_outstanding,
            }
        )

    if not values:
        return result

    stmt = insert(FundamentalDaily).values(values)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[FundamentalDaily.instrument_id, FundamentalDaily.date],
            set_={
                "marketcap_cr": stmt.excluded.marketcap_cr,
                "pe": stmt.excluded.pe,
                "pb": stmt.excluded.pb,
                "div_yield": stmt.excluded.div_yield,
                "shares_outstanding": stmt.excluded.shares_outstanding,
            },
        )
    )
    result.rows_written = len(values)
    return result


def _pe_on(
    quoted_pe: Decimal | None, close_raw: Decimal | None, last_price: Decimal | None
) -> Decimal | None:
    """Re-price the quoted P/E onto ``on``'s exchange print. House rule 5.

    NSE's ``quote-equity`` has no history: it answers with *today's* price and *today's* P/E.
    A fill that runs on a Tuesday for the previous Friday would therefore store Tuesday's price
    inside a Friday row, and a backtest standing on Friday would be reading the future. That is
    look-ahead, and CLAUDE.md's rule 5 warns explicitly against adding a second one.

    So only the earnings half of the ratio is carried over. ``pe = price / eps``, so the implied
    ``eps = last_price / pe``, and the P/E *on* the target date is ``close_raw / eps``, which
    reduces to scaling the quoted ratio by the two prices. Every price in the stored row is then
    the price the exchange actually printed that day.

    **The residual, stated rather than hidden:** the EPS vintage is still today's. If a company
    published results between ``on`` and the fetch, the stored ratio uses earnings that were not
    known on ``on``. NSE publishes no point-in-time EPS series, so this is the floor, not a
    choice — and it is a far smaller error than carrying the price across too. A same-day
    nightly run has neither problem; this matters only for a backfill of a past date.
    """
    if quoted_pe is None or quoted_pe <= 0:
        return quoted_pe
    if close_raw is None or last_price is None or close_raw <= 0 or last_price <= 0:
        return quoted_pe
    return (quoted_pe * close_raw / last_price).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _marketcap_cr(
    shares: int | None, close_raw: Decimal | None, last_price: Decimal | None
) -> int | None:
    """Prefer the exchange print; fall back to the quote's last price."""
    price = close_raw if close_raw is not None and close_raw > 0 else last_price
    if shares is None or price is None or price <= 0:
        return None
    return int((Decimal(shares) * price / _CRORE).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
