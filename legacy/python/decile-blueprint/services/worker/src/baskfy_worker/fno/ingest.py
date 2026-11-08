"""The nightly F&O bhavcopy ingest and the ban list (``docs/fno/03`` §1-§2, ``04`` §4, FO2).

One session's F&O bhavcopy, read through ``NSEProvider.fo_bhavcopy`` (archive-then-parse, the NSE
limiter, the wrong-date refusal), trimmed by ``03`` §1's retention, rounded at write time (house
rule 8) and upserted into ``fo_contract_daily`` by its primary key with the raw file's
``source_key`` (house rule 7: a re-run writes identical rows). Every attempt is recorded in
``fo_ingest_day``: ``PENDING`` while the 18:30-23:30 retries run, ``INGESTED`` when the rows are
written, ``MISSING`` when the last attempt finds no file — never interpolated (``04`` §4).

The same night stores the ban list NSE published **for the next session** (``02`` Track C §9):
on ``fo_ingest_day`` (the list, its session and its archive key) and as ``in_ban`` on tonight's
``fo_underlying_daily`` rows ("the F&O ban list for the next session", ``03`` §2).

RETENTION (``03`` §1)
---------------------
Every future. For options: every contract of the **two nearest monthly expiries** of each
underlying, plus NIFTY's and BANKNIFTY's **weeklies**, with non-zero OI or volume. "Monthly" is
the last listed expiry of its calendar month for that underlying — read from the file, never a
weekday rule (the options pack's ``04`` §1.1, carried). The raw zip stays in the archive, so a
trimmed row is re-derivable without asking NSE again.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Protocol

import polars as pl
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import FoContractDaily, FoIngestDay, FoUnderlyingDaily
from baskfy_core.models.base import JsonObject
from baskfy_core.models.fno import FO_FUTURE_INSTRUMENTS, FUTURE_TYPE
from baskfy_core.precision import PRICE_DP, quantise
from baskfy_providers.archive import archive_key
from baskfy_providers.errors import ProviderError
from baskfy_providers.nse import KIND_FO_BAN_LIST, KIND_FO_BHAVCOPY
from baskfy_worker.fno.partitions import ensure_contract_partition

log = logging.getLogger("baskfy_worker.fno.ingest")

#: ``03`` §1: the two underlyings whose weeklies are kept as well as their monthlies.
WEEKLY_UNDERLYINGS: Final[frozenset[str]] = frozenset({"NIFTY", "BANKNIFTY"})
#: ``03`` §1: "every contract of the two nearest monthly expiries of each underlying".
NEAREST_MONTHLIES: Final = 2
#: ``04`` §4: the nightly retries hourly to 23:30; a day with no file by then is MISSING.
LAST_ATTEMPT_IST: Final = dt.time(23, 30)
#: Rows per INSERT statement: ~17 columns each, well inside asyncpg's 32,767 parameter cap.
BATCH: Final = 1_000

STATUS_PENDING: Final = "PENDING"
STATUS_INGESTED: Final = "INGESTED"
STATUS_MISSING: Final = "MISSING"


class FoBhavcopyReader(Protocol):
    """The two NSE reads the ingest needs — ``NSEProvider`` satisfies it."""

    def fo_bhavcopy(self, on: dt.date) -> pl.DataFrame: ...

    def fo_ban_list(self, for_session: dt.date) -> list[str]: ...


@dataclass(slots=True)
class IngestReport:
    trade_date: dt.date
    status: str
    rows_in_file: int | None = None
    rows_kept: int | None = None
    source_key: str | None = None
    error: str | None = None
    skipped: str | None = None

    def as_dict(self) -> JsonObject:
        return {
            "trade_date": self.trade_date.isoformat(),
            "status": self.status,
            "rows_in_file": self.rows_in_file,
            "rows_kept": self.rows_kept,
            "source_key": self.source_key,
            "error": self.error,
            "skipped": self.skipped,
        }


@dataclass(slots=True)
class BanReport:
    for_session: dt.date
    symbols: list[str] | None = None
    source_key: str | None = None
    error: str | None = None
    skipped: str | None = None

    def as_dict(self) -> JsonObject:
        return {
            "for_session": self.for_session.isoformat(),
            "symbols": self.symbols,
            "source_key": self.source_key,
            "error": self.error,
            "skipped": self.skipped,
        }


# ------------------------------------------------------------------------------------------------
# Pure: retention and the row shape
# ------------------------------------------------------------------------------------------------


def monthly_expiries(frame: pl.DataFrame) -> pl.DataFrame:
    """``(symbol, expiry)`` for every monthly expiry in the file: the last listed expiry of its
    calendar month for that underlying. No weekday arithmetic."""
    return (
        frame.select("symbol", "expiry")
        .unique()
        .with_columns(
            (pl.col("expiry").dt.year() * 100 + pl.col("expiry").dt.month()).alias("month")
        )
        .group_by("symbol", "month")
        .agg(pl.col("expiry").max())
        .select("symbol", "expiry")
    )


def retain(frame: pl.DataFrame) -> pl.DataFrame:
    """``03`` §1's retention over one session's ``FO_BHAVCOPY_SCHEMA`` frame. Pure."""
    if frame.is_empty():
        return frame
    futures = frame.filter(pl.col("instrument").is_in(list(FO_FUTURE_INSTRUMENTS)))
    options = frame.filter(~pl.col("instrument").is_in(list(FO_FUTURE_INSTRUMENTS)))
    monthly = monthly_expiries(frame)
    nearest = (
        monthly.join(frame.select("symbol", "date").unique(), on="symbol")
        .filter(pl.col("expiry") >= pl.col("date"))
        .sort("symbol", "expiry")
        .with_columns(pl.int_range(pl.len()).over("symbol").alias("rank"))
        .filter(pl.col("rank") < NEAREST_MONTHLIES)
        .select("symbol", "expiry", pl.lit(value=True).alias("near_monthly"))
    )
    is_monthly = monthly.with_columns(pl.lit(value=True).alias("is_monthly"))
    traded = (pl.col("open_interest").fill_null(0) > 0) | (pl.col("volume").fill_null(0) > 0)
    weekly = pl.col("symbol").is_in(sorted(WEEKLY_UNDERLYINGS)) & pl.col("is_monthly").is_null()
    kept = (
        options.join(nearest, on=["symbol", "expiry"], how="left")
        .join(is_monthly, on=["symbol", "expiry"], how="left")
        .filter(traded & (pl.col("near_monthly").is_not_null() | weekly))
        .drop("near_monthly", "is_monthly")
    )
    return pl.concat([futures, kept], how="vertical").sort(
        "symbol", "expiry", "instrument", "strike", "option_type", nulls_last=True
    )


def contract_rows(frame: pl.DataFrame, source_key: str) -> list[dict[str, object]]:
    """Retained rows as ``fo_contract_daily`` values, rounded at write time (house rule 8).

    A future's missing strike becomes 0 and its option type ``XX``, so the key has no null.
    """
    out: list[dict[str, object]] = []
    for row in frame.iter_rows(named=True):
        option_type = row["option_type"]
        is_future = row["instrument"] in FO_FUTURE_INSTRUMENTS
        out.append(
            {
                "trade_date": row["date"],
                "instrument": row["instrument"],
                "symbol": row["symbol"],
                "expiry": row["expiry"],
                "strike": quantise(row["strike"] or 0, PRICE_DP),
                "option_type": FUTURE_TYPE if is_future or option_type is None else option_type,
                "open": quantise(row["open"], PRICE_DP),
                "high": quantise(row["high"], PRICE_DP),
                "low": quantise(row["low"], PRICE_DP),
                "close": quantise(row["close"], PRICE_DP),
                "settle": quantise(row["settle"], PRICE_DP),
                "underlying": quantise(row["underlying"], PRICE_DP),
                "open_interest": row["open_interest"],
                "oi_change": row["oi_change"],
                "volume": row["volume"],
                "turnover": quantise(row["turnover"], PRICE_DP),
                "lot_size": row["lot_size"],
                "source_key": source_key,
            }
        )
    return out


def is_final_attempt(trade_date: dt.date, now_ist: dt.datetime) -> bool:
    """Whether 23:30 IST on ``trade_date`` has passed: a failure now is ``MISSING``, not
    ``PENDING`` (``04`` §4). A past day's attempt is always final."""
    naive = now_ist.replace(tzinfo=None)
    return naive >= dt.datetime.combine(trade_date, LAST_ATTEMPT_IST)


# ------------------------------------------------------------------------------------------------
# Database
# ------------------------------------------------------------------------------------------------


async def ingested(session: AsyncSession, trade_date: dt.date) -> bool:
    status = await session.scalar(
        select(FoIngestDay.status).where(FoIngestDay.trade_date == trade_date)
    )
    return status == STATUS_INGESTED


async def ingested_days(session: AsyncSession, days: Sequence[dt.date]) -> set[dt.date]:
    if not days:
        return set()
    rows = await session.execute(
        select(FoIngestDay.trade_date).where(
            FoIngestDay.trade_date.in_(list(days)), FoIngestDay.status == STATUS_INGESTED
        )
    )
    return {row[0] for row in rows}


async def upsert_contracts(session: AsyncSession, rows: Sequence[dict[str, object]]) -> None:
    """Idempotent on ``(trade_date, symbol, expiry, strike, option_type)``."""
    for start in range(0, len(rows), BATCH):
        stmt = insert(FoContractDaily).values(list(rows[start : start + BATCH]))
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=["trade_date", "symbol", "expiry", "strike", "option_type"],
                set_={
                    name: stmt.excluded[name]
                    for name in (
                        "instrument",
                        "open",
                        "high",
                        "low",
                        "close",
                        "settle",
                        "underlying",
                        "open_interest",
                        "oi_change",
                        "volume",
                        "turnover",
                        "lot_size",
                        "source_key",
                    )
                },
            )
        )


async def _record(session: AsyncSession, report: IngestReport, *, attempted: bool) -> None:
    values: dict[str, object] = {
        "trade_date": report.trade_date,
        "status": report.status,
        "rows_in_file": report.rows_in_file,
        "rows_kept": report.rows_kept,
        "source_key": report.source_key,
        "attempts": 1 if attempted else 0,
        "last_error": report.error,
    }
    stmt = insert(FoIngestDay).values(values)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["trade_date"],
            set_={
                "status": stmt.excluded.status,
                "rows_in_file": stmt.excluded.rows_in_file,
                "rows_kept": stmt.excluded.rows_kept,
                "source_key": stmt.excluded.source_key,
                "attempts": FoIngestDay.attempts + stmt.excluded.attempts,
                "last_error": stmt.excluded.last_error,
                "updated_at": func.now(),
            },
        )
    )


async def ingest_day(
    session: AsyncSession,
    reader: FoBhavcopyReader,
    trade_date: dt.date,
    *,
    final_attempt: bool,
) -> IngestReport:
    """One session's bhavcopy into ``fo_contract_daily``; the attempt into ``fo_ingest_day``.

    A day already ``INGESTED`` is answered without a read. A provider failure (no file yet, a
    wrong-date file, NSE down after its retries) is recorded with its message — ``PENDING`` before
    the last attempt, ``MISSING`` on it — and returned, not raised: the retry is the schedule's.
    """
    if await ingested(session, trade_date):
        return IngestReport(trade_date, STATUS_INGESTED, skipped="already ingested")
    try:
        frame = reader.fo_bhavcopy(trade_date)
    except ProviderError as exc:
        status = STATUS_MISSING if final_attempt else STATUS_PENDING
        report = IngestReport(trade_date, status, error=f"{type(exc).__name__}: {exc}")
        log.warning("fno bhavcopy %s: %s (%s)", trade_date.isoformat(), status, report.error)
        await _record(session, report, attempted=True)
        return report
    kept = retain(frame)
    key = archive_key(KIND_FO_BHAVCOPY, trade_date, "zip")
    await ensure_contract_partition(session, trade_date)
    await upsert_contracts(session, contract_rows(kept, key))
    report = IngestReport(
        trade_date,
        STATUS_INGESTED,
        rows_in_file=frame.height,
        rows_kept=kept.height,
        source_key=key,
    )
    await _record(session, report, attempted=True)
    await session.flush()
    return report


async def store_ban_list(
    session: AsyncSession,
    reader: FoBhavcopyReader,
    trade_date: dt.date,
    for_session: dt.date,
) -> BanReport:
    """The ban list for ``for_session`` onto ``trade_date``'s rows (``03`` §2 ``in_ban``).

    Answered without a read when tonight's row already holds that session's list. A refusal (not
    yet published, the wrong date) is returned, not raised; the next hourly run asks again.
    """
    stored = (
        await session.execute(
            select(FoIngestDay.ban_for_session, FoIngestDay.ban_symbols).where(
                FoIngestDay.trade_date == trade_date
            )
        )
    ).first()
    if stored is not None and stored[0] == for_session and stored[1] is not None:
        return BanReport(for_session, symbols=list(stored[1]), skipped="already stored")
    try:
        symbols = reader.fo_ban_list(for_session)
    except ProviderError as exc:
        return BanReport(for_session, error=f"{type(exc).__name__}: {exc}")
    key = archive_key(KIND_FO_BAN_LIST, for_session, "csv")
    stmt = insert(FoIngestDay).values(
        trade_date=trade_date,
        status=STATUS_PENDING,
        ban_for_session=for_session,
        ban_symbols=symbols,
        ban_source_key=key,
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["trade_date"],
            set_={
                "ban_for_session": stmt.excluded.ban_for_session,
                "ban_symbols": stmt.excluded.ban_symbols,
                "ban_source_key": stmt.excluded.ban_source_key,
                "updated_at": func.now(),
            },
        )
    )
    await session.execute(
        update(FoUnderlyingDaily)
        .where(FoUnderlyingDaily.trade_date == trade_date)
        .values(in_ban=FoUnderlyingDaily.symbol.in_(symbols) if symbols else False)
    )
    if symbols:
        banned = insert(FoUnderlyingDaily).values(
            [{"trade_date": trade_date, "symbol": s, "in_ban": True} for s in symbols]
        )
        await session.execute(
            banned.on_conflict_do_update(
                index_elements=["trade_date", "symbol"], set_={"in_ban": True}
            )
        )
    await session.flush()
    return BanReport(for_session, symbols=symbols, source_key=key)
