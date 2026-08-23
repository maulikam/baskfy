"""Persist and recompute curated-basket catalog metrics (SC2).

Loads **every** version a basket has ever had plus their constituents, chain-links the daily
returns across the version boundaries in ``baskfy_core.curated_metrics``, and upserts
``cb_metrics``. Idempotent per ``(basket_id, as_of_date)``.

Three things this service deliberately does not do any more (SC-hardening):

* It does not resolve one "latest" version and replay today's weights over five years of
  prices. That is look-ahead (CLAUDE.md house rule 5) and it overstated the 5Y CAGR of a
  simulated momentum catalog by 15.59pp. :func:`baskfy_core.curated_metrics.version_aware_nav`
  walks the versions forward instead.
* It does not index windows off the end of whatever dates happen to exist. "1Y" is a calendar
  offset resolved against the basket's own trading days (docs/05 section Notation); 252 bars
  land 373-378 calendar days back. Nor does it pass a nominal ``years=3`` to a slice that spans
  3.01 years.
* It does not quantise NAV inside the compounding loop. House rule 8 is round at *write* time,
  and the intermediate NAV is never written at all.

Cost note: since-inception is anchored at ``launched_at``, so the price fetch reaches back to
the basket's first version rather than to a fixed five-year window. An eleven-year basket costs
an eleven-year read once a day; truncating it to five years and calling the answer
"since inception" is the alternative, and it is a lie.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.curated_metrics import (
    CARD_WINDOW_MONTHS,
    MIN_BASKET_VOL_DAYS,
    BasketVersion,
    CatalogVolatility,
    catalog_volatility,
    constituent_volatilities,
    min_amount,
    return_convention_fields,
    since_inception_anchor,
    since_inception_return,
    version_aware_nav,
    whole_months_between,
    window_cagr,
    window_return,
)
from baskfy_core.gst import money
from baskfy_core.models import (
    CbBasket,
    CbBasketVersion,
    CbConstituent,
    CbMetrics,
    Instrument,
    OhlcvDaily,
)

#: SC11 / leaf-1.8.4 - process baskets in bounded chunks (no unbounded ORM load).
METRICS_BASKET_CHUNK: Final = 50


async def upsert_metrics_row(  # noqa: PLR0913 - one kwarg per cb_metrics column
    session: AsyncSession,
    *,
    basket_id: int,
    as_of: dt.date,
    min_amt: Decimal | None,
    vol_bucket: str | None,
    vol_value: Decimal | None,
    ret_1m: Decimal | None,
    ret_6m: Decimal | None,
    ret_1y: Decimal | None,
    cagr_3y: Decimal | None,
    cagr_5y: Decimal | None,
    since_inception_pct: Decimal | None,
    computed_at: dt.datetime,
) -> None:
    """Insert or update one ``cb_metrics`` row. Re-running the same inputs is a no-op."""
    values = {
        "basket_id": basket_id,
        "as_of_date": as_of,
        "min_amount": min_amt,
        "volatility_bucket": vol_bucket,
        "volatility_value": vol_value,
        "ret_1m": ret_1m,
        "ret_6m": ret_6m,
        "ret_1y": ret_1y,
        "cagr_3y": cagr_3y,
        "cagr_5y": cagr_5y,
        "since_inception_pct": since_inception_pct,
        "computed_at": computed_at,
    }
    stmt = insert(CbMetrics).values(**values)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[CbMetrics.basket_id, CbMetrics.as_of_date],
            set_={
                "min_amount": stmt.excluded.min_amount,
                "volatility_bucket": stmt.excluded.volatility_bucket,
                "volatility_value": stmt.excluded.volatility_value,
                "ret_1m": stmt.excluded.ret_1m,
                "ret_6m": stmt.excluded.ret_6m,
                "ret_1y": stmt.excluded.ret_1y,
                "cagr_3y": stmt.excluded.cagr_3y,
                "cagr_5y": stmt.excluded.cagr_5y,
                "since_inception_pct": stmt.excluded.since_inception_pct,
                "computed_at": stmt.excluded.computed_at,
            },
        )
    )


async def _versions_through(
    session: AsyncSession, basket_id: int, as_of: dt.date
) -> list[CbBasketVersion]:
    """Every version effective on or before *as_of*, oldest first.

    All of them, not the latest one: the basket's history is the history of the weights it
    actually held, and the ones it no longer holds are most of it.
    """
    rows = (
        await session.execute(
            select(CbBasketVersion)
            .where(
                CbBasketVersion.basket_id == basket_id,
                CbBasketVersion.effective_date <= as_of,
            )
            .order_by(CbBasketVersion.effective_date, CbBasketVersion.version_no)
        )
    ).scalars()
    return list(rows)


async def _constituents_by_version(
    session: AsyncSession, version_ids: Sequence[int]
) -> dict[int, list[tuple[int, Decimal, str]]]:
    """``{version_id: [(instrument_id, weight, symbol)]}`` in one query, not N."""
    if not version_ids:
        return {}
    rows = (
        await session.execute(
            select(
                CbConstituent.version_id,
                CbConstituent.instrument_id,
                CbConstituent.weight,
                Instrument.symbol,
            )
            .join(Instrument, Instrument.id == CbConstituent.instrument_id)
            .where(CbConstituent.version_id.in_(list(version_ids)))
        )
    ).all()
    out: dict[int, list[tuple[int, Decimal, str]]] = {}
    for version_id, instrument_id, weight, symbol in rows:
        out.setdefault(int(version_id), []).append(
            (int(instrument_id), Decimal(weight), str(symbol))
        )
    return out


async def _close_raw_map(
    session: AsyncSession, instrument_ids: Sequence[int], as_of: dt.date
) -> dict[int, Decimal]:
    if not instrument_ids:
        return {}
    # Latest close_raw on or before as_of for each instrument.
    out: dict[int, Decimal] = {}
    for iid in instrument_ids:
        row = (
            await session.execute(
                select(OhlcvDaily.close_raw)
                .where(OhlcvDaily.instrument_id == iid, OhlcvDaily.date <= as_of)
                .order_by(OhlcvDaily.date.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is not None:
            out[iid] = Decimal(row)
    return out


async def _price_history(
    session: AsyncSession, instrument_ids: Sequence[int], start: dt.date, as_of: dt.date
) -> dict[dt.date, dict[str, Decimal]]:
    """``{date: {symbol: close}}`` for adjusted closes over ``[start, as_of]``.

    A calendar range, not a bar count: the window resolution downstream needs the real trading
    days, and ``lookback * 1.7`` days of guesswork is what made "5Y" mean 5.12 years.
    """
    if not instrument_ids:
        return {}
    rows = (
        await session.execute(
            select(OhlcvDaily.date, Instrument.symbol, OhlcvDaily.close)
            .join(Instrument, Instrument.id == OhlcvDaily.instrument_id)
            .where(
                OhlcvDaily.instrument_id.in_(list(instrument_ids)),
                OhlcvDaily.date >= start,
                OhlcvDaily.date <= as_of,
            )
            .order_by(OhlcvDaily.date)
        )
    ).all()
    by_date: dict[dt.date, dict[str, Decimal]] = {}
    for trade_date, symbol, close in rows:
        by_date.setdefault(trade_date, {})[str(symbol)] = Decimal(close)
    return by_date


@dataclass(frozen=True, slots=True)
class BasketMetricValues:
    """Everything computable for one basket without knowing its peers.

    The volatility bucket is deliberately absent: it depends on the published catalog's tercile
    set, so it is assigned in a second pass once every basket's value is known.
    """

    basket_id: int
    visibility: str
    min_amount: Decimal | None
    volatility: CatalogVolatility | None
    ret_1m: Decimal | None
    ret_6m: Decimal | None
    ret_1y: Decimal | None
    cagr_3y: Decimal | None
    cagr_5y: Decimal | None
    since_inception_pct: Decimal | None
    months_available: int | None
    status: str


def _weights_by_symbol(
    constituents: Sequence[tuple[int, Decimal, str]],
) -> dict[str, Decimal]:
    return {symbol: weight for _, weight, symbol in constituents}


def _min_amount_for(
    constituents: Sequence[tuple[int, Decimal, str]], prices: Mapping[int, Decimal]
) -> Decimal | None:
    if not constituents or len(prices) != len(constituents):
        return None
    ordered_prices = [prices[iid] for iid, _, _ in constituents]
    ordered_weights = [weight for _, weight, _ in constituents]
    return money(min_amount(ordered_prices, ordered_weights))


async def _metric_values(
    session: AsyncSession, basket: CbBasket, as_of: dt.date
) -> BasketMetricValues:
    """Compute one basket's numbers. No peers, no bucket, no write."""
    empty = BasketMetricValues(
        basket_id=basket.id,
        visibility=basket.visibility,
        min_amount=None,
        volatility=None,
        ret_1m=None,
        ret_6m=None,
        ret_1y=None,
        cagr_3y=None,
        cagr_5y=None,
        since_inception_pct=None,
        months_available=None,
        status="no_version",
    )
    versions = await _versions_through(session, basket.id, as_of)
    if not versions:
        return empty

    constituents = await _constituents_by_version(session, [v.id for v in versions])
    latest = versions[-1]
    latest_constituents = constituents.get(latest.id, [])
    prices = await _close_raw_map(session, [c[0] for c in latest_constituents], as_of)

    history_versions = [
        BasketVersion(
            effective_date=version.effective_date,
            weights=_weights_by_symbol(constituents.get(version.id, [])),
        )
        for version in versions
        if constituents.get(version.id)
    ]
    all_instrument_ids = sorted({iid for rows in constituents.values() for iid, _, _ in rows})
    inception = versions[0].effective_date
    history = await _price_history(session, all_instrument_ids, inception, as_of)
    nav = version_aware_nav(history, history_versions)

    latest_weights = _weights_by_symbol(latest_constituents)
    basket_returns = nav.returns_only
    # docs/smallcase/04 section 3's fallback is only reached below 60 trading days, and it costs
    # a pass over the whole price history per constituent - so it is computed only when it can
    # actually be used.
    constituent_vols = (
        constituent_volatilities(history, sorted(latest_weights))
        if len(basket_returns) < MIN_BASKET_VOL_DAYS
        else None
    )
    volatility = catalog_volatility(
        basket_daily_returns=basket_returns,
        weights=latest_weights,
        constituent_vols=constituent_vols,
    )
    launched_at = basket.launched_at
    # The label a card shows for a young basket names this many months, so it has to be the
    # span the number is measured over - the launch snapped forward into the series, not the
    # basket's nominal age over a record that starts later.
    anchor = since_inception_anchor(nav.points, launched_at=launched_at)
    return BasketMetricValues(
        basket_id=basket.id,
        visibility=basket.visibility,
        min_amount=_min_amount_for(latest_constituents, prices),
        volatility=volatility,
        ret_1m=window_return(nav.points, months=CARD_WINDOW_MONTHS["1m"]),
        ret_6m=window_return(nav.points, months=CARD_WINDOW_MONTHS["6m"]),
        ret_1y=window_return(nav.points, months=CARD_WINDOW_MONTHS["1y"]),
        cagr_3y=window_cagr(nav.points, months=CARD_WINDOW_MONTHS["3y"]),
        cagr_5y=window_cagr(nav.points, months=CARD_WINDOW_MONTHS["5y"]),
        since_inception_pct=since_inception_return(nav.points, launched_at=launched_at),
        months_available=(whole_months_between(anchor, as_of) if anchor is not None else None),
        status="ok",
    )


@dataclass(frozen=True, slots=True)
class CatalogContext:
    """What a row needs from the rest of the catalog: the tercile set, and one clock read."""

    published_count: int
    peer_vols: tuple[Decimal, ...] | None
    computed_at: dt.datetime


async def _write_metric_values(
    session: AsyncSession,
    values: BasketMetricValues,
    as_of: dt.date,
    context: CatalogContext,
) -> dict[str, object]:
    """Assign the bucket (the one number that needs the peer set) and upsert."""
    bucket = (
        values.volatility.bucket(
            published_count=context.published_count, peer_vols=context.peer_vols
        )
        if values.volatility is not None
        else None
    )
    await upsert_metrics_row(
        session,
        basket_id=values.basket_id,
        as_of=as_of,
        min_amt=values.min_amount,
        vol_bucket=bucket,
        vol_value=values.volatility.value if values.volatility is not None else None,
        ret_1m=values.ret_1m,
        ret_6m=values.ret_6m,
        ret_1y=values.ret_1y,
        cagr_3y=values.cagr_3y,
        cagr_5y=values.cagr_5y,
        since_inception_pct=values.since_inception_pct,
        computed_at=context.computed_at,
    )
    payload: dict[str, object] = {
        "basket_id": values.basket_id,
        "as_of": as_of.isoformat(),
        "status": values.status,
        "min_amount": str(values.min_amount) if values.min_amount is not None else None,
        "volatility_bucket": bucket,
        "volatility_basis": values.volatility.basis if values.volatility is not None else None,
        "months_available": values.months_available,
    }
    # A6: the catalog's returns exclude dividends. Said here so the payload carries the
    # convention instead of leaving every reader to assume total return.
    payload.update(return_convention_fields())
    return payload


async def compute_basket_metrics(  # noqa: PLR0913 - published_count/peers for PACK.1
    session: AsyncSession,
    basket: CbBasket,
    as_of: dt.date,
    *,
    published_count: int,
    peer_vols: Sequence[Decimal] | None = None,
    now: dt.datetime | None = None,
) -> dict[str, object]:
    """Compute one basket's catalog metrics for *as_of* and upsert the row."""
    context = CatalogContext(
        published_count=published_count,
        peer_vols=tuple(peer_vols) if peer_vols is not None else None,
        computed_at=now or dt.datetime.now(tz=dt.UTC),
    )
    values = await _metric_values(session, basket, as_of)
    return await _write_metric_values(session, values, as_of, context)


async def compute_all_metrics(
    session: AsyncSession,
    as_of: dt.date,
    *,
    now: dt.datetime | None = None,
) -> dict[str, object]:
    """EOD job body: upsert metrics for every non-archived basket on *as_of*.

    Two passes. The first computes each basket's numbers, loading baskets in
    ``METRICS_BASKET_CHUNK``-sized batches so a large catalog cannot pin an unbounded ORM set in
    memory (SC11 / leaf-1.8.4); only the small value objects are kept. The second assigns the
    volatility bucket, which needs the terciles of the PUBLISHED catalog and therefore cannot be
    known until the first pass is done - before SC-hardening ``peer_vols`` was never passed at
    all and the tercile branch was dead code.
    """
    computed_at = now or dt.datetime.now(tz=dt.UTC)
    basket_ids = list(
        (
            await session.execute(
                select(CbBasket.id).where(CbBasket.archived_at.is_(None)).order_by(CbBasket.id)
            )
        )
        .scalars()
        .all()
    )
    published_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(CbBasket)
                .where(
                    CbBasket.archived_at.is_(None),
                    CbBasket.visibility == "PUBLISHED",
                )
            )
        ).scalar_one()
    )

    computed: list[BasketMetricValues] = []
    chunk_size = METRICS_BASKET_CHUNK
    for offset in range(0, len(basket_ids), chunk_size):
        chunk_ids = basket_ids[offset : offset + chunk_size]
        baskets = (
            (
                await session.execute(
                    select(CbBasket).where(CbBasket.id.in_(chunk_ids)).order_by(CbBasket.id)
                )
            )
            .scalars()
            .all()
        )
        for basket in baskets:
            computed.append(await _metric_values(session, basket, as_of))

    peer_vols = [
        values.volatility.value
        for values in computed
        if values.visibility == "PUBLISHED" and values.volatility is not None
    ]

    context = CatalogContext(
        published_count=published_count,
        peer_vols=tuple(peer_vols),
        computed_at=computed_at,
    )
    results: list[dict[str, object]] = []
    for index, values in enumerate(computed, start=1):
        results.append(await _write_metric_values(session, values, as_of, context))
        if index % chunk_size == 0:
            await session.flush()
    await session.flush()
    await session.commit()
    summary: dict[str, object] = {
        "as_of": as_of.isoformat(),
        "baskets": len(results),
        "results": results,
    }
    summary.update(return_convention_fields())
    return summary
