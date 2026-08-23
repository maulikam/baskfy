"""Persist and recompute curated-basket catalog metrics (SC2).

Loads versions + prices, calls ``baskfy_core.curated_metrics``, upserts ``cb_metrics``.
Idempotent per ``(basket_id, as_of_date)``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from decimal import Decimal
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.curated_metrics import (
    absolute_return,
    annualized_volatility,
    basket_day_return,
    cagr,
    min_amount,
    volatility_bucket_for_catalog,
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

#: Trading-day windows for card returns (approximate NSE counts; SC4 may refine).
WINDOW_1M: Final = 21
WINDOW_6M: Final = 126
WINDOW_1Y: Final = 252
WINDOW_3Y: Final = 756
WINDOW_5Y: Final = 1260
#: docs/smallcase/04 section 3: below 60 trading days, fall back to constituent-weighted vol.
MIN_BASKET_VOL_DAYS: Final = 60
MIN_NAV_POINTS: Final = 2
#: SC11 / leaf-1.8.4 — process baskets in bounded chunks (no unbounded ORM load).
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


async def _latest_version(
    session: AsyncSession, basket_id: int, as_of: dt.date
) -> CbBasketVersion | None:
    return (
        await session.execute(
            select(CbBasketVersion)
            .where(
                CbBasketVersion.basket_id == basket_id,
                CbBasketVersion.effective_date <= as_of,
            )
            .order_by(CbBasketVersion.effective_date.desc(), CbBasketVersion.version_no.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _constituents(session: AsyncSession, version_id: int) -> list[tuple[int, Decimal, str]]:
    rows = (
        await session.execute(
            select(CbConstituent.instrument_id, CbConstituent.weight, Instrument.symbol)
            .join(Instrument, Instrument.id == CbConstituent.instrument_id)
            .where(CbConstituent.version_id == version_id)
        )
    ).all()
    return [(int(r[0]), Decimal(r[1]), str(r[2])) for r in rows]


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
    session: AsyncSession, instrument_ids: Sequence[int], as_of: dt.date, lookback: int
) -> dict[dt.date, dict[str, Decimal]]:
    """``{date: {symbol: close}}`` for adjusted closes over a lookback window."""
    if not instrument_ids:
        return {}
    # Pull a generous calendar window; filter to rows we have.
    start = as_of - dt.timedelta(days=int(lookback * 1.7) + 30)
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
    computed_at = now or dt.datetime.now(tz=dt.UTC)
    version = await _latest_version(session, basket.id, as_of)
    if version is None:
        await upsert_metrics_row(
            session,
            basket_id=basket.id,
            as_of=as_of,
            min_amt=None,
            vol_bucket=None,
            vol_value=None,
            ret_1m=None,
            ret_6m=None,
            ret_1y=None,
            cagr_3y=None,
            cagr_5y=None,
            since_inception_pct=None,
            computed_at=computed_at,
        )
        return {"basket_id": basket.id, "as_of": as_of.isoformat(), "status": "no_version"}

    constituents = await _constituents(session, version.id)
    instrument_ids = [c[0] for c in constituents]
    weights_by_symbol = {c[2]: c[1] for c in constituents}
    prices = await _close_raw_map(session, instrument_ids, as_of)

    min_amt: Decimal | None = None
    if len(prices) == len(constituents) and constituents:
        ordered_prices = [prices[iid] for iid, _, _ in constituents]
        ordered_weights = [w for _, w, _ in constituents]
        min_amt = money(min_amount(ordered_prices, ordered_weights))

    history = await _price_history(session, instrument_ids, as_of, WINDOW_5Y + 5)
    dates = sorted(history)
    daily_returns: list[Decimal] = []
    for i in range(1, len(dates)):
        daily_returns.append(
            basket_day_return(weights_by_symbol, history[dates[i - 1]], history[dates[i]])
        )

    # NAV index from daily returns (base 100 on first return day).
    navs: list[Decimal] = []
    nav = Decimal("100")
    for ret in daily_returns:
        nav = money(nav * (Decimal("1") + ret))
        navs.append(nav)

    def _window_return(trading_days: int) -> Decimal | None:
        if len(navs) < trading_days:
            return None
        return absolute_return(navs[-trading_days], navs[-1])

    ret_1m = _window_return(WINDOW_1M)
    ret_6m = _window_return(WINDOW_6M)
    ret_1y = _window_return(WINDOW_1Y)
    since = absolute_return(navs[0], navs[-1]) if len(navs) >= MIN_NAV_POINTS else None

    cagr_3y = None
    cagr_5y = None
    if len(navs) >= WINDOW_3Y:
        cagr_3y = cagr(nav_start=navs[-WINDOW_3Y], nav_end=navs[-1], years=Decimal("3"))
    if len(navs) >= WINDOW_5Y:
        cagr_5y = cagr(nav_start=navs[-WINDOW_5Y], nav_end=navs[-1], years=Decimal("5"))

    vol_slice = (
        daily_returns[-WINDOW_1Y:] if len(daily_returns) >= MIN_BASKET_VOL_DAYS else daily_returns
    )
    vol_value = annualized_volatility(vol_slice)
    vol_bucket = (
        volatility_bucket_for_catalog(
            vol_value, published_count=published_count, peer_vols=peer_vols
        )
        if vol_value is not None
        else None
    )

    await upsert_metrics_row(
        session,
        basket_id=basket.id,
        as_of=as_of,
        min_amt=min_amt,
        vol_bucket=vol_bucket,
        vol_value=vol_value,
        ret_1m=ret_1m,
        ret_6m=ret_6m,
        ret_1y=ret_1y,
        cagr_3y=cagr_3y,
        cagr_5y=cagr_5y,
        since_inception_pct=since,
        computed_at=computed_at,
    )
    return {
        "basket_id": basket.id,
        "as_of": as_of.isoformat(),
        "status": "ok",
        "min_amount": str(min_amt) if min_amt is not None else None,
        "volatility_bucket": vol_bucket,
    }


async def compute_all_metrics(
    session: AsyncSession,
    as_of: dt.date,
    *,
    now: dt.datetime | None = None,
) -> dict[str, object]:
    """EOD job body: upsert metrics for every non-archived basket on *as_of*.

    Baskets are loaded in ``METRICS_BASKET_CHUNK``-sized batches so a large catalog
    cannot pin an unbounded ORM set in memory (SC11 / leaf-1.8.4).
    """
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
    results: list[dict[str, object]] = []
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
            results.append(
                await compute_basket_metrics(
                    session,
                    basket,
                    as_of,
                    published_count=published_count,
                    now=now,
                )
            )
        await session.flush()
    await session.commit()
    return {"as_of": as_of.isoformat(), "baskets": len(results), "results": results}
