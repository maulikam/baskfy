"""Set-based EOD materialization for the curated-basket catalog.

The service reads only PostgreSQL facts already published by the nightly pipeline. It never calls
a provider or imports the execution package. All baskets are validated and computed before the
first ORM mutation, so one incomplete history cannot leave earlier baskets partially persisted.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from sqlalchemy import BigInteger, Date, column, exists, select
from sqlalchemy import values as sql_values
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.curated_metrics import (
    YOUNG_BASKET_RETURN_DAYS,
    AdjustedClose,
    ConstituentVolatility,
    CuratedMetricsError,
    DailyAdjustedCloses,
    PublishedVolatility,
    TargetWeight,
    VersionWeights,
    WeightedPrice,
    annualized_volatility,
    assign_volatility_buckets,
    basket_volatility,
    chain_linked_nav,
    minimum_investment_amount,
    nav_daily_returns,
    round_money,
    round_return,
    window_metrics,
)
from baskfy_core.models import (
    BasketConstituent,
    BasketMetrics,
    BasketVersion,
    CuratedBasket,
    OhlcvDaily,
    PipelineRun,
    TradingDay,
)
from baskfy_core.seed_data import NSE_EXCHANGE_ID

PRICE_PREFETCH_CALENDAR_DAYS: Final = 400
MAX_PUBLISHED_BASKETS: Final = 500
MAX_PRICE_ROWS: Final = 250_000
MAX_PRICE_INTERVALS: Final = 10_000
PRICE_STREAM_BATCH: Final = 5_000
MIN_CONSTITUENT_RETURN_COUNT: Final = 2


class CuratedMetricsJobError(RuntimeError):
    """Published database facts cannot produce a complete, truthful metrics snapshot."""


@dataclass(frozen=True, slots=True)
class CuratedMetricsJobResult:
    as_of_date: dt.date
    status: str
    baskets: int
    written: int
    unchanged: int
    price_rows: int

    def as_dict(self) -> dict[str, object]:
        return {
            "as_of_date": self.as_of_date.isoformat(),
            "status": self.status,
            "baskets": self.baskets,
            "written": self.written,
            "unchanged": self.unchanged,
            "price_rows": self.price_rows,
        }


@dataclass(frozen=True, slots=True)
class _BasketDefinition:
    basket_id: int
    launched_at: dt.date
    versions: tuple[VersionWeights, ...]

    @property
    def instrument_ids(self) -> frozenset[int]:
        return frozenset(
            row.instrument_id for version in self.versions for row in version.constituents
        )


@dataclass(frozen=True, slots=True)
class _PriceStore:
    by_day: dict[dt.date, dict[int, tuple[Decimal, Decimal]]]
    by_instrument: dict[int, tuple[tuple[dt.date, Decimal], ...]]
    row_count: int


@dataclass(frozen=True, slots=True)
class _DesiredMetric:
    basket_id: int
    min_amount: Decimal
    volatility_value: Decimal
    ret_1m: Decimal | None
    ret_6m: Decimal | None
    ret_1y: Decimal | None
    cagr_3y: Decimal | None
    cagr_5y: Decimal | None
    since_inception_pct: Decimal


async def compute_curated_metrics(
    session: AsyncSession,
    as_of_date: dt.date,
) -> CuratedMetricsJobResult:
    """Compute and converge one published trading day's ``cb_metrics`` rows."""
    if type(as_of_date) is not dt.date:
        raise CuratedMetricsJobError("as_of_date must be a date, not a datetime")

    trading_day, published = (
        await session.execute(
            select(
                exists().where(
                    TradingDay.exchange_id == NSE_EXCHANGE_ID,
                    TradingDay.date == as_of_date,
                    TradingDay.is_trading_day.is_(True),
                ),
                exists().where(
                    PipelineRun.trade_date == as_of_date,
                    PipelineRun.status == "succeeded",
                    PipelineRun.data_version.is_not(None),
                ),
            )
        )
    ).one()
    if not trading_day:
        return CuratedMetricsJobResult(as_of_date, "not-trading-day", 0, 0, 0, 0)
    if not published:
        return CuratedMetricsJobResult(as_of_date, "not-published", 0, 0, 0, 0)

    definitions = await _load_definitions(session, as_of_date)
    if not definitions:
        return CuratedMetricsJobResult(as_of_date, "computed", 0, 0, 0, 0)
    first_day = min(row.versions[0].effective_date for row in definitions)
    history_floor = first_day - dt.timedelta(days=PRICE_PREFETCH_CALENDAR_DAYS)
    trading_days = tuple(
        (
            await session.scalars(
                select(TradingDay.date)
                .where(
                    TradingDay.exchange_id == NSE_EXCHANGE_ID,
                    TradingDay.is_trading_day.is_(True),
                    TradingDay.date >= history_floor,
                    TradingDay.date <= as_of_date,
                )
                .order_by(TradingDay.date)
            )
        ).all()
    )
    if not trading_days or trading_days[-1] != as_of_date:
        raise CuratedMetricsJobError(f"trading calendar does not cover {as_of_date.isoformat()}")

    prices = await _load_prices(session, definitions, trading_days, as_of_date)
    desired = tuple(
        _compute_one(definition, trading_days, prices, as_of_date) for definition in definitions
    )
    buckets = assign_volatility_buckets(
        tuple(PublishedVolatility(row.basket_id, row.volatility_value) for row in desired)
    )
    written, unchanged = await _converge_metrics(
        session,
        as_of_date,
        desired,
        {basket_id: bucket.value for basket_id, bucket in buckets.items()},
    )
    return CuratedMetricsJobResult(
        as_of_date,
        "computed",
        len(desired),
        written,
        unchanged,
        prices.row_count,
    )


async def _load_definitions(
    session: AsyncSession, as_of_date: dt.date
) -> tuple[_BasketDefinition, ...]:
    baskets = (
        (
            await session.execute(
                select(CuratedBasket.id, CuratedBasket.launched_at)
                .where(
                    CuratedBasket.visibility == "PUBLISHED",
                    CuratedBasket.archived_at.is_(None),
                    CuratedBasket.launched_at <= as_of_date,
                )
                .order_by(CuratedBasket.id)
            )
        )
        .tuples()
        .all()
    )
    if len(baskets) > MAX_PUBLISHED_BASKETS:
        raise CuratedMetricsJobError(
            f"published basket count exceeds bounded limit {MAX_PUBLISHED_BASKETS}"
        )
    if not baskets:
        return ()

    basket_ids = [basket_id for basket_id, _launched_at in baskets]
    rows = (
        (
            await session.execute(
                select(
                    BasketVersion.basket_id,
                    BasketVersion.id,
                    BasketVersion.effective_date,
                    BasketConstituent.instrument_id,
                    BasketConstituent.weight,
                )
                .join(BasketConstituent, BasketConstituent.version_id == BasketVersion.id)
                .where(
                    BasketVersion.basket_id.in_(basket_ids),
                    BasketVersion.effective_date <= as_of_date,
                )
                .order_by(
                    BasketVersion.basket_id,
                    BasketVersion.effective_date,
                    BasketConstituent.instrument_id,
                )
            )
        )
        .tuples()
        .all()
    )
    versions_by_basket: dict[int, dict[tuple[int, dt.date], list[TargetWeight]]] = {
        basket_id: {} for basket_id in basket_ids
    }
    for basket_id, version_id, effective_date, instrument_id, weight in rows:
        versions_by_basket[basket_id].setdefault((version_id, effective_date), []).append(
            TargetWeight(instrument_id, weight)
        )

    definitions: list[_BasketDefinition] = []
    for basket_id, launched_at in baskets:
        grouped = versions_by_basket[basket_id]
        if not grouped:
            raise CuratedMetricsJobError(
                f"published basket {basket_id} has no version effective by {as_of_date.isoformat()}"
            )
        definitions.append(
            _BasketDefinition(
                basket_id=basket_id,
                launched_at=launched_at,
                versions=tuple(
                    VersionWeights(effective_date, tuple(constituents))
                    for (_version_id, effective_date), constituents in grouped.items()
                ),
            )
        )
    return tuple(definitions)


async def _load_prices(
    session: AsyncSession,
    definitions: tuple[_BasketDefinition, ...],
    trading_days: tuple[dt.date, ...],
    as_of_date: dt.date,
) -> _PriceStore:
    interval_rows = _bounded_price_intervals(definitions, trading_days, as_of_date)
    interval_values = (
        sql_values(
            column("instrument_id", BigInteger),
            column("start_date", Date),
            column("end_date", Date),
            name="required_price_interval",
        )
        .data(interval_rows)
        .alias("required_price_interval")
    )
    statement = (
        select(
            OhlcvDaily.date,
            OhlcvDaily.instrument_id,
            OhlcvDaily.close,
            OhlcvDaily.close_raw,
        )
        .join(
            interval_values,
            (interval_values.c.instrument_id == OhlcvDaily.instrument_id)
            & (OhlcvDaily.date >= interval_values.c.start_date)
            & (OhlcvDaily.date <= interval_values.c.end_date),
        )
        .distinct()
        .order_by(OhlcvDaily.date, OhlcvDaily.instrument_id)
        .execution_options(yield_per=PRICE_STREAM_BATCH, stream_results=True)
    )
    result = await session.stream(statement)
    by_day: dict[dt.date, dict[int, tuple[Decimal, Decimal]]] = {}
    by_instrument_lists: dict[int, list[tuple[dt.date, Decimal]]] = {}
    row_count = 0
    async for day, instrument_id, adjusted, raw in result.tuples():
        row_count += 1
        if row_count > MAX_PRICE_ROWS:
            raise CuratedMetricsJobError(f"price history exceeds bounded limit {MAX_PRICE_ROWS}")
        by_day.setdefault(day, {})[instrument_id] = (adjusted, raw)
        by_instrument_lists.setdefault(instrument_id, []).append((day, adjusted))
    return _PriceStore(
        by_day=by_day,
        by_instrument={key: tuple(value) for key, value in by_instrument_lists.items()},
        row_count=row_count,
    )


def _price_intervals(
    definitions: tuple[_BasketDefinition, ...],
    trading_days: tuple[dt.date, ...],
    as_of_date: dt.date,
) -> list[tuple[int, dt.date, dt.date]]:
    """Merge only active-version intervals plus the current 400-day volatility fallback."""
    day_index = {day: index for index, day in enumerate(trading_days)}
    raw: dict[int, list[tuple[dt.date, dt.date]]] = {}
    for definition in definitions:
        for index, version in enumerate(definition.versions):
            active_start = next(day for day in trading_days if day >= version.effective_date)
            start_index = max(0, day_index[active_start] - 1)
            if index + 1 < len(definition.versions):
                next_start = next(
                    day
                    for day in trading_days
                    if day >= definition.versions[index + 1].effective_date
                )
                end_index = max(start_index, day_index[next_start] - 1)
                active_end = trading_days[end_index]
            else:
                active_end = as_of_date
            for constituent in version.constituents:
                raw.setdefault(constituent.instrument_id, []).append(
                    (trading_days[start_index], active_end)
                )

        first_effective = next(
            day for day in trading_days if day >= definition.versions[0].effective_date
        )
        basket_return_days = day_index[as_of_date] - day_index[first_effective]
        if basket_return_days < YOUNG_BASKET_RETURN_DAYS:
            current = _version_on(definition.versions, as_of_date)
            fallback_start = as_of_date - dt.timedelta(days=PRICE_PREFETCH_CALENDAR_DAYS)
            for constituent in current.constituents:
                raw.setdefault(constituent.instrument_id, []).append((fallback_start, as_of_date))

    merged_rows: list[tuple[int, dt.date, dt.date]] = []
    for instrument_id, intervals in raw.items():
        merged: list[tuple[dt.date, dt.date]] = []
        for start, end in sorted(intervals):
            if merged and start <= merged[-1][1] + dt.timedelta(days=1):
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        merged_rows.extend((instrument_id, start, end) for start, end in merged)
    return merged_rows


def _bounded_price_intervals(
    definitions: tuple[_BasketDefinition, ...],
    trading_days: tuple[dt.date, ...],
    as_of_date: dt.date,
) -> list[tuple[int, dt.date, dt.date]]:
    rows = _price_intervals(definitions, trading_days, as_of_date)
    if len(rows) > MAX_PRICE_INTERVALS:
        raise CuratedMetricsJobError(
            f"price interval count exceeds bounded limit {MAX_PRICE_INTERVALS}"
        )
    return rows


def _compute_one(
    definition: _BasketDefinition,
    trading_days: tuple[dt.date, ...],
    prices: _PriceStore,
    as_of_date: dt.date,
) -> _DesiredMetric:
    first_effective = next(
        day for day in trading_days if day >= definition.versions[0].effective_date
    )
    first_expected = next((day for day in trading_days if day >= definition.launched_at), None)
    if first_expected is None or first_effective != first_expected:
        raise CuratedMetricsJobError(
            f"basket {definition.basket_id} launch/version history does not start on its first "
            "effective trading day"
        )
    performance_days = tuple(day for day in trading_days if day >= first_effective)
    _validate_performance_coverage(definition, performance_days, prices)
    price_days = tuple(
        DailyAdjustedCloses(
            day,
            tuple(
                AdjustedClose(instrument_id, prices.by_day[day][instrument_id][0])
                for instrument_id in _required_close_ids(
                    definition.versions, performance_days, index
                )
            ),
        )
        for index, day in enumerate(performance_days)
    )
    try:
        nav = chain_linked_nav(definition.versions, price_days)
        returns = nav_daily_returns(nav)
        active = _version_on(definition.versions, as_of_date)
        weighted_prices = tuple(
            WeightedPrice(
                row.instrument_id,
                row.weight,
                prices.by_day[as_of_date][row.instrument_id][1],
            )
            for row in active.constituents
        )
        fallback = (
            _constituent_volatilities(active, trading_days, prices, as_of_date)
            if len(returns) < YOUNG_BASKET_RETURN_DAYS
            else ()
        )
        volatility = basket_volatility(returns, fallback)
        windows = window_metrics(nav)
    except CuratedMetricsError as exc:
        raise CuratedMetricsJobError(f"basket {definition.basket_id}: {exc}") from exc

    return _DesiredMetric(
        basket_id=definition.basket_id,
        min_amount=round_money(minimum_investment_amount(weighted_prices)),
        volatility_value=round_return(volatility),
        ret_1m=_round_optional(windows.ret_1m),
        ret_6m=_round_optional(windows.ret_6m),
        ret_1y=_round_optional(windows.ret_1y),
        cagr_3y=_round_optional(windows.cagr_3y),
        cagr_5y=_round_optional(windows.cagr_5y),
        since_inception_pct=round_return(windows.since_inception_pct),
    )


def _required_close_ids(
    versions: Sequence[VersionWeights], performance_days: tuple[dt.date, ...], index: int
) -> tuple[int, ...]:
    """Current constituents plus tomorrow's, whose return needs today's denominator."""
    required = {
        row.instrument_id for row in _version_on(versions, performance_days[index]).constituents
    }
    if index + 1 < len(performance_days):
        required.update(
            row.instrument_id
            for row in _version_on(versions, performance_days[index + 1]).constituents
        )
    return tuple(sorted(required))


def _validate_performance_coverage(
    definition: _BasketDefinition,
    performance_days: tuple[dt.date, ...],
    prices: _PriceStore,
) -> None:
    if not performance_days:
        raise CuratedMetricsJobError(f"basket {definition.basket_id} has no performance dates")
    previous_day: dt.date | None = None
    for day in performance_days:
        active_ids = {
            row.instrument_id for row in _version_on(definition.versions, day).constituents
        }
        available = prices.by_day.get(day, {})
        missing = active_ids - available.keys()
        if previous_day is not None:
            missing |= active_ids - prices.by_day.get(previous_day, {}).keys()
        if missing:
            raise CuratedMetricsJobError(
                f"basket {definition.basket_id} has incomplete adjusted-close coverage on "
                f"{day.isoformat()} for instruments {sorted(missing)}"
            )
        previous_day = day


def _version_on(versions: Sequence[VersionWeights], day: dt.date) -> VersionWeights:
    active = versions[0]
    for version in versions[1:]:
        if version.effective_date > day:
            break
        active = version
    return active


def _constituent_volatilities(
    active: VersionWeights,
    trading_days: tuple[dt.date, ...],
    prices: _PriceStore,
    as_of_date: dt.date,
) -> tuple[ConstituentVolatility, ...]:
    result: list[ConstituentVolatility] = []
    for constituent in active.constituents:
        fallback_start = as_of_date - dt.timedelta(days=PRICE_PREFETCH_CALENDAR_DAYS)
        observations = tuple(
            row
            for row in prices.by_instrument.get(constituent.instrument_id, ())
            if row[0] >= fallback_start
        )
        if not observations or observations[-1][0] != as_of_date:
            raise CuratedMetricsJobError(
                f"missing current adjusted close for instrument {constituent.instrument_id}"
            )
        observed_days = tuple(day for day, _close in observations)
        expected_days = tuple(day for day in trading_days if day >= observed_days[0])
        if observed_days != expected_days:
            raise CuratedMetricsJobError(
                f"incomplete constituent-volatility coverage for instrument "
                f"{constituent.instrument_id}"
            )
        closes = tuple(close for _day, close in observations[-253:])
        daily_returns = tuple(
            closes[index] / closes[index - 1] - 1 for index in range(1, len(closes))
        )
        if len(daily_returns) < MIN_CONSTITUENT_RETURN_COUNT:
            raise CuratedMetricsJobError(
                f"insufficient constituent-volatility history for instrument "
                f"{constituent.instrument_id}"
            )
        result.append(
            ConstituentVolatility(
                constituent.instrument_id,
                constituent.weight,
                annualized_volatility(daily_returns),
            )
        )
    return tuple(result)


def _round_optional(value: Decimal | None) -> Decimal | None:
    return None if value is None else round_return(value)


async def _converge_metrics(
    session: AsyncSession,
    as_of_date: dt.date,
    desired: tuple[_DesiredMetric, ...],
    buckets: dict[int, str],
) -> tuple[int, int]:
    existing = {
        row.basket_id: row
        for row in (
            await session.scalars(
                select(BasketMetrics).where(
                    BasketMetrics.as_of_date == as_of_date,
                    BasketMetrics.basket_id.in_([row.basket_id for row in desired]),
                )
            )
        ).all()
    }
    written = 0
    unchanged = 0
    now = dt.datetime.now(dt.UTC)
    for values in desired:
        row = existing.get(values.basket_id)
        content = _metric_content(values, buckets[values.basket_id])
        if row is None:
            session.add(
                BasketMetrics(
                    basket_id=values.basket_id,
                    as_of_date=as_of_date,
                    **content,
                )
            )
            written += 1
        elif _stored_metric_content(row) == content:
            unchanged += 1
        else:
            for field, value in content.items():
                setattr(row, field, value)
            row.computed_at = now
            written += 1
    if written:
        await session.flush()
    return written, unchanged


def _metric_content(values: _DesiredMetric, bucket: str) -> dict[str, Decimal | str | None]:
    return {
        "min_amount": values.min_amount,
        "volatility_bucket": bucket,
        "volatility_value": values.volatility_value,
        "ret_1m": values.ret_1m,
        "ret_6m": values.ret_6m,
        "ret_1y": values.ret_1y,
        "cagr_3y": values.cagr_3y,
        "cagr_5y": values.cagr_5y,
        "since_inception_pct": values.since_inception_pct,
    }


def _stored_metric_content(row: BasketMetrics) -> dict[str, Decimal | str | None]:
    return {
        "min_amount": row.min_amount,
        "volatility_bucket": row.volatility_bucket,
        "volatility_value": row.volatility_value,
        "ret_1m": row.ret_1m,
        "ret_6m": row.ret_6m,
        "ret_1y": row.ret_1y,
        "cagr_3y": row.cagr_3y,
        "cagr_5y": row.cagr_5y,
        "since_inception_pct": row.since_inception_pct,
    }
