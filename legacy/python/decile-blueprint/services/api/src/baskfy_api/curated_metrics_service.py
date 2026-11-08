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
"since inception" is the alternative, and it is a lie. What the job does instead is pay that
read **once per chunk** rather than once per basket: a chunk's price history is one query for
the union of its constituents, bounded below by the earliest anchor in the chunk and sliced per
basket in memory (:func:`_slice_history`). Catalogs overlap heavily — the same large-caps sit in
most baskets — so the union is a fraction of the sum.

Transaction shape: one transaction per chunk of reads and one per written basket, never one for
the whole catalog. A single long transaction pins the xmin horizon for its whole duration, which
stops TimescaleDB compressing ``ohlcv_daily`` chunks while the job runs. Committing per basket
also makes a partial run resumable: the upsert is idempotent per ``(basket_id, as_of_date)`` and
the basket cursor is ordered by id, so a re-run redoes finished work as a no-op and finishes the
rest.
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
    DIVIDENDS_INCLUDED,
    MIN_BASKET_VOL_DAYS,
    RETURN_CONVENTION,
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

#: SC11 / leaf-1.8.4 - process baskets in bounded chunks (no unbounded ORM load). It is also the
#: unit of the shared price load below, so it bounds how much history is held in memory at once.
METRICS_BASKET_CHUNK: Final = 50

#: How far back the "latest close_raw on or before as_of" lookup may scan. ``ohlcv_daily`` is a
#: TimescaleDB hypertable with one-year chunks: an unbounded ``date <= as_of`` makes the planner
#: consider every chunk of every year, and that per-statement planning - not the row count - is
#: what made this lookup cost 19.24ms per basket. A calendar month covers any run of exchange
#: holidays; an instrument with no print for a month has no current price worth putting on a
#: card, and ``min_amount`` is then NULL rather than a stale number presented as today's.
CLOSE_RAW_LOOKBACK_DAYS: Final = 30


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
    vol_basis: str | None = None,
    months_available: int | None = None,
    return_convention: str = RETURN_CONVENTION,
    dividends_included: bool = DIVIDENDS_INCLUDED,
) -> None:
    """Insert or update one ``cb_metrics`` row. Re-running the same inputs is a no-op.

    Every column the row has is written, including the four the compute used to drop on the
    floor: an UPDATE that leaves ``volatility_basis`` at yesterday's value while replacing
    ``volatility_value`` publishes a number labelled with the wrong measurement.
    """
    values = {
        "basket_id": basket_id,
        "as_of_date": as_of,
        "min_amount": min_amt,
        "volatility_bucket": vol_bucket,
        "volatility_value": vol_value,
        "volatility_basis": vol_basis,
        "months_available": months_available,
        "return_convention": return_convention,
        "dividends_included": dividends_included,
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
                "volatility_basis": stmt.excluded.volatility_basis,
                "months_available": stmt.excluded.months_available,
                "return_convention": stmt.excluded.return_convention,
                "dividends_included": stmt.excluded.dividends_included,
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


@dataclass(frozen=True, slots=True)
class BasketRef:
    """The three ``cb_basket`` columns the metrics compute actually reads.

    A value, not a ``CbBasket``: the job commits between baskets, and a commit expires every ORM
    instance in the session — reading ``launched_at`` off one afterwards silently re-SELECTs the
    row, which is the N+1 this refactor exists to remove. Values survive a commit; ORM rows do
    not.
    """

    id: int
    visibility: str
    launched_at: dt.date | None

    @classmethod
    def of(cls, basket: CbBasket) -> BasketRef:
        return cls(id=basket.id, visibility=basket.visibility, launched_at=basket.launched_at)


@dataclass(frozen=True, slots=True)
class _VersionRow:
    """One ``cb_basket_version``, reduced to what the chain-link needs."""

    id: int
    effective_date: dt.date


@dataclass(frozen=True, slots=True)
class _ChunkInputs:
    """Every row a chunk of baskets needs — four queries, however many baskets are in it.

    ``history`` is the union over the chunk's constituents; each basket takes its own slice of
    it in memory (:func:`_slice_history`) instead of issuing its own read.
    """

    versions: Mapping[int, tuple[_VersionRow, ...]]
    constituents: Mapping[int, list[tuple[int, Decimal, str]]]
    history: Mapping[dt.date, Mapping[str, Decimal]]
    close_raw: Mapping[int, Decimal]


async def _versions_by_basket(
    session: AsyncSession, basket_ids: Sequence[int], as_of: dt.date
) -> dict[int, tuple[_VersionRow, ...]]:
    """``{basket_id: (versions effective on or before as_of, oldest first)}`` in one query.

    All of them, not the latest one: the basket's history is the history of the weights it
    actually held, and the ones it no longer holds are most of it.
    """
    if not basket_ids:
        return {}
    rows = (
        await session.execute(
            select(
                CbBasketVersion.basket_id,
                CbBasketVersion.id,
                CbBasketVersion.effective_date,
            )
            .where(
                CbBasketVersion.basket_id.in_(list(basket_ids)),
                CbBasketVersion.effective_date <= as_of,
            )
            .order_by(
                CbBasketVersion.basket_id,
                CbBasketVersion.effective_date,
                CbBasketVersion.version_no,
            )
        )
    ).all()
    out: dict[int, list[_VersionRow]] = {}
    for basket_id, version_id, effective_date in rows:
        out.setdefault(int(basket_id), []).append(
            _VersionRow(id=int(version_id), effective_date=effective_date)
        )
    return {basket_id: tuple(versions) for basket_id, versions in out.items()}


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
    """Latest ``close_raw`` on or before *as_of* per instrument — ONE bounded query.

    Two things make this cheap and neither is enough alone. ``DISTINCT ON (instrument_id)``
    turns what was a statement per instrument into a statement per chunk; the
    :data:`CLOSE_RAW_LOOKBACK_DAYS` floor keeps the planner off every year-chunk of the
    hypertable. Batching without the floor measured no better than the loop it replaced.
    """
    if not instrument_ids:
        return {}
    floor = as_of - dt.timedelta(days=CLOSE_RAW_LOOKBACK_DAYS)
    rows = (
        await session.execute(
            select(OhlcvDaily.instrument_id, OhlcvDaily.close_raw)
            .distinct(OhlcvDaily.instrument_id)
            .where(
                OhlcvDaily.instrument_id.in_(list(instrument_ids)),
                OhlcvDaily.date >= floor,
                OhlcvDaily.date <= as_of,
            )
            .order_by(OhlcvDaily.instrument_id, OhlcvDaily.date.desc())
        )
    ).all()
    return {int(instrument_id): Decimal(close_raw) for instrument_id, close_raw in rows}


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


def _slice_history(
    history: Mapping[dt.date, Mapping[str, Decimal]],
    symbols: frozenset[str],
    start: dt.date,
) -> dict[dt.date, dict[str, Decimal]]:
    """One basket's view of the chunk-wide load: its symbols, from its own anchor.

    Exactly the rows its own ``WHERE instrument_id IN (...) AND date >= start`` would have
    returned — a date on which none of *symbols* printed is dropped, as the query would have
    dropped it, so a peer basket's trading day cannot invent a day for this one.
    """
    sliced: dict[dt.date, dict[str, Decimal]] = {}
    for trade_date, closes in history.items():
        if trade_date < start:
            continue
        row = {symbol: close for symbol, close in closes.items() if symbol in symbols}
        if row:
            sliced[trade_date] = row
    return sliced


async def _load_chunk(
    session: AsyncSession, refs: Sequence[BasketRef], as_of: dt.date
) -> _ChunkInputs:
    """Load a whole chunk of baskets in four queries.

    The price read is bounded below by the EARLIEST version date in the chunk (leaf 2d): the
    anchoring fix means an eleven-year basket needs eleven years, and paying that once for the
    chunk is the price of it. Constituents overlap heavily between baskets, so the union is far
    smaller than the sum — that duplication is what this removes.
    """
    versions = await _versions_by_basket(session, [ref.id for ref in refs], as_of)
    version_ids = [row.id for rows in versions.values() for row in rows]
    constituents = await _constituents_by_version(session, version_ids)

    instrument_ids = sorted(
        {instrument_id for rows in constituents.values() for instrument_id, _, _ in rows}
    )
    starts = [rows[0].effective_date for rows in versions.values() if rows]
    history = await _price_history(session, instrument_ids, min(starts), as_of) if starts else {}

    latest_instrument_ids = sorted(
        {
            instrument_id
            for rows in versions.values()
            if rows
            for instrument_id, _, _ in constituents.get(rows[-1].id, [])
        }
    )
    close_raw = await _close_raw_map(session, latest_instrument_ids, as_of)
    return _ChunkInputs(
        versions=versions, constituents=constituents, history=history, close_raw=close_raw
    )


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
    """The smallest whole-share amount, or NULL when any one name is unpriced.

    The guard is "every constituent has a price", **not** "the map is the same size as the
    basket". ``prices`` is built once per chunk and covers every instrument across every basket
    in it, so a size comparison is only ever true by accident — it held while ``cb_basket`` had a
    single 15-name row and returned NULL for all six baskets the moment the catalogue was filled.
    A basket cannot be priced off a map that happens to be the right length.
    """
    if not constituents:
        return None
    ordered_prices: list[Decimal] = []
    for instrument_id, _, _ in constituents:
        price = prices.get(instrument_id)
        if price is None:
            # One unpriced name means the whole minimum is unknown, and NULL says so rather than
            # quoting a figure that would buy an incomplete basket.
            return None
        ordered_prices.append(price)
    ordered_weights = [weight for _, weight, _ in constituents]
    return money(min_amount(ordered_prices, ordered_weights))


def _metric_values(basket: BasketRef, inputs: _ChunkInputs, as_of: dt.date) -> BasketMetricValues:
    """Compute one basket's numbers. No peers, no bucket, no write — and no query.

    Every row it reads was loaded once for the whole chunk, so this is pure arithmetic over
    memory: 750 round-trips per job became four per chunk.
    """
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
    versions = inputs.versions.get(basket.id, ())
    if not versions:
        return empty

    constituents = inputs.constituents
    latest_constituents = constituents.get(versions[-1].id, [])

    history_versions = [
        BasketVersion(
            effective_date=version.effective_date,
            weights=_weights_by_symbol(constituents.get(version.id, [])),
        )
        for version in versions
        if constituents.get(version.id)
    ]
    symbols = frozenset(
        symbol for version in versions for _, _, symbol in constituents.get(version.id, [])
    )
    history = _slice_history(inputs.history, symbols, versions[0].effective_date)
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
        min_amount=_min_amount_for(latest_constituents, inputs.close_raw),
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
        vol_basis=values.volatility.basis if values.volatility is not None else None,
        months_available=values.months_available,
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
    ref = BasketRef.of(basket)
    inputs = await _load_chunk(session, [ref], as_of)
    return await _write_metric_values(session, _metric_values(ref, inputs, as_of), as_of, context)


async def _basket_refs(session: AsyncSession, basket_ids: Sequence[int]) -> list[BasketRef]:
    """The chunk's baskets as values, ordered by id so a resumed run has a stable cursor."""
    rows = (
        await session.execute(
            select(CbBasket.id, CbBasket.visibility, CbBasket.launched_at)
            .where(CbBasket.id.in_(list(basket_ids)))
            .order_by(CbBasket.id)
        )
    ).all()
    return [
        BasketRef(id=int(basket_id), visibility=str(visibility), launched_at=launched_at)
        for basket_id, visibility, launched_at in rows
    ]


async def compute_all_metrics(
    session: AsyncSession,
    as_of: dt.date,
    *,
    now: dt.datetime | None = None,
) -> dict[str, object]:
    """EOD job body: upsert metrics for every non-archived basket on *as_of*.

    Two passes. The first computes each basket's numbers, loading baskets in
    ``METRICS_BASKET_CHUNK``-sized batches so a large catalog cannot pin an unbounded ORM set in
    memory (SC11 / leaf-1.8.4); only the small value objects are kept. Each chunk's rows -
    versions, constituents, the union of their prices, the closing prints - are read in four
    queries and then sliced per basket in memory. The second pass assigns the volatility bucket,
    which needs the terciles of the PUBLISHED catalog and therefore cannot be known until the
    first pass is done - before SC-hardening ``peer_vols`` was never passed at all and the
    tercile branch was dead code.

    The transaction is per chunk while reading and per basket while writing, never one for the
    whole run: a job-long transaction holds the xmin horizon down and blocks ``ohlcv_daily``
    compression for as long as it runs. Because the upsert is idempotent per
    ``(basket_id, as_of_date)`` and the cursor is ordered by ``cb_basket.id``, a run that dies
    half-way leaves the finished baskets committed and a re-run rewrites them identically.
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
        refs = await _basket_refs(session, chunk_ids)
        inputs = await _load_chunk(session, refs, as_of)
        # The chunk's reads are done and its rows are values now, so end the transaction rather
        # than hold one snapshot across the whole catalog.
        await session.commit()
        computed.extend(_metric_values(ref, inputs, as_of) for ref in refs)

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
    for values in computed:
        results.append(await _write_metric_values(session, values, as_of, context))
        # Per basket, so a run that dies has finished baskets on disk and resumes from the next.
        await session.commit()
    summary: dict[str, object] = {
        "as_of": as_of.isoformat(),
        "baskets": len(results),
        "results": results,
    }
    summary.update(return_convention_fields())
    return summary
