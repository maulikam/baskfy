"""The quarterly re-test's I/O (``docs/fno/04`` §6, FO9): ``fo_contract_daily`` in,
``fo_backtest_run`` out.

The arithmetic is :mod:`baskfy_core.fno.retest` (pure). This module reads the futures rows whole
(~700,000 rows for 2022-2026) and the option rows **a batch of symbols at a time**, so the box
never holds the whole option panel (B1 over the whole panel peaks at 2.8 GB; DECISIONS-FO FO9.3).
The pure run is synchronous, so it runs in a thread and its loader calls back into this event
loop for each batch.

F3's two families (``F3N``/``F3B``) read one index at a time through :func:`load_index_rows`: its
FUTIDX rows from the futures already loaded and its OPTIDX rows **with the weeklies** — the
option panel above keeps only the expiries that have a future, which drops every NIFTY weekly.

Every row is appended (``fo_backtest_run`` is append-only; the page reads the latest per family)
for every FO tenant (the users with ``fo_sleeve_config`` rows, FO4.2), with its tier, its caveat
verbatim and its slippage source. Nothing it writes moves a flag or a plan.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Sequence
from decimal import Decimal
from typing import Final

import polars as pl
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.fno import research
from baskfy_core.fno import retest as rt
from baskfy_core.models import FoBacktestRun, FoContractDaily, FoSpreadSample
from baskfy_core.models.base import JsonObject
from baskfy_worker.fno.scan import scan_users
from baskfy_worker.fno.spreads import SpreadObservation, half_spread_stats

#: The re-test starts where the research did (``RESEARCH.md``: 3 Jan 2022).
SAMPLE_FROM: Final = dt.date(2022, 1, 3)
#: A worker process runs this many symbols at a time (smaller than the pure default: the box
#: shares its 7.8 GB with Postgres and every other service).
BOX_SYMBOL_BATCH: Final = 10

_FUTURES = ("FUTSTK", "FUTIDX")
_OPTIONS = ("OPTSTK", "OPTIDX")
_FUTURE_SELECT = (
    FoContractDaily.trade_date,
    FoContractDaily.instrument,
    FoContractDaily.symbol,
    FoContractDaily.expiry,
    FoContractDaily.open,
    FoContractDaily.high,
    FoContractDaily.low,
    FoContractDaily.close,
    FoContractDaily.settle,
    FoContractDaily.underlying,
    FoContractDaily.open_interest,
    FoContractDaily.oi_change,
    FoContractDaily.volume,
    FoContractDaily.turnover,
    FoContractDaily.lot_size,
)
_OPTION_SELECT = (
    FoContractDaily.trade_date,
    FoContractDaily.instrument,
    FoContractDaily.symbol,
    FoContractDaily.expiry,
    FoContractDaily.strike,
    FoContractDaily.option_type,
    FoContractDaily.close,
    FoContractDaily.settle,
    FoContractDaily.open_interest,
    FoContractDaily.volume,
)
_FLOATS = ("open", "high", "low", "close", "settle", "underlying", "turnover", "strike")


def _frame(rows: Sequence[Sequence[object]], names: Sequence[str]) -> pl.DataFrame:
    """Rows as the research's panel frame: ``date`` for ``trade_date``, floats for prices."""
    frame = pl.DataFrame([tuple(r) for r in rows], schema=list(names), orient="row", strict=False)
    frame = frame.rename({"trade_date": "date"})
    return frame.with_columns(
        pl.col(c).cast(pl.Float64) for c in _FLOATS if c in frame.columns
    ).with_columns(
        pl.col(c).cast(pl.Int64)
        for c in ("open_interest", "oi_change", "volume", "lot_size")
        if c in frame.columns
    )


async def load_futures(session: AsyncSession, start: dt.date, end: dt.date) -> pl.DataFrame:
    rows = (
        await session.execute(
            select(*_FUTURE_SELECT).where(
                FoContractDaily.instrument.in_(_FUTURES),
                FoContractDaily.trade_date >= start,
                FoContractDaily.trade_date <= end,
            )
        )
    ).all()
    return _frame(rows, [c.key for c in _FUTURE_SELECT])


async def load_options(
    session: AsyncSession,
    futures: pl.DataFrame,
    symbols: frozenset[str],
    start: dt.date,
    end: dt.date,
) -> pl.DataFrame:
    """``panel.py``'s option rows for ``symbols``: the pure filter applied to what the table has.

    The table already keeps only the two nearest monthlies (plus the index weeklies) with OI or
    volume (``03`` §1), so the query narrows by symbol and date and the pure
    :func:`~baskfy_core.fno.retest.panels_from_contracts` does the rest exactly as the research
    did.
    """
    rows = (
        await session.execute(
            select(*_OPTION_SELECT).where(
                FoContractDaily.instrument.in_(_OPTIONS),
                FoContractDaily.symbol.in_(sorted(symbols)),
                FoContractDaily.trade_date >= start,
                FoContractDaily.trade_date <= end,
            )
        )
    ).all()
    if not rows:
        return rt.empty_options()
    options = _frame(rows, [c.key for c in _OPTION_SELECT])
    mine = futures.filter(pl.col("symbol").is_in(sorted(symbols)))
    return rt.panels_from_contracts(pl.concat([mine, options], how="diagonal_relaxed")).options


async def load_index_rows(
    session: AsyncSession, futures: pl.DataFrame, symbol: str, start: dt.date, end: dt.date
) -> pl.DataFrame:
    """``symbol``'s raw rows for F3's proxy: its futures, and its index options of every listed
    expiry within :data:`~baskfy_core.fno.retest.DIRECTIONAL_EXPIRY_DAYS` of the session (the
    pure trim then narrows the strikes)."""
    rows = (
        await session.execute(
            select(*_OPTION_SELECT).where(
                FoContractDaily.instrument == "OPTIDX",
                FoContractDaily.symbol == symbol,
                FoContractDaily.trade_date >= start,
                FoContractDaily.trade_date <= end,
                FoContractDaily.expiry <= FoContractDaily.trade_date + rt.DIRECTIONAL_EXPIRY_DAYS,
            )
        )
    ).all()
    mine = futures.filter((pl.col("symbol") == symbol) & (pl.col("instrument") == "FUTIDX"))
    if not rows:
        return mine
    options = _frame(rows, [c.key for c in _OPTION_SELECT])
    return pl.concat([mine, options], how="diagonal_relaxed")


async def measured_spreads(session: AsyncSession) -> dict[str, tuple[float, int]]:
    """FO3's statistic per underlying: (median half-spread ÷ mid, sessions)."""
    rows = (await session.execute(select(FoSpreadSample))).scalars().all()
    stats = half_spread_stats(
        SpreadObservation(
            trade_date=s.trade_date,
            symbol=s.symbol,
            expiry=s.expiry,
            strike=s.strike,
            option_type=s.option_type,
            bid=s.bid,
            ask=s.ask,
            taken_at=s.taken_at,
        )
        for s in rows
    )
    return {k: (float(v.median_half_spread_pct_of_mid), v.sessions) for k, v in stats.items()}


async def last_run(session: AsyncSession) -> dt.date | None:
    got = (await session.execute(select(func.max(FoBacktestRun.run_at)))).scalar_one_or_none()
    return got.date() if got is not None else None


def _row(user_id: int, res: rt.FamilyResult) -> FoBacktestRun:
    return FoBacktestRun(
        user_id=user_id,
        family=res.family,
        params=res.params,
        tier="2E",
        caveat=res.caveat,
        sample_from=res.sample_from,
        sample_to=res.sample_to,
        n=res.n,
        net_r=None if res.net_r is None else Decimal(str(res.net_r)),
        gross_r=None if res.gross_r is None else Decimal(str(res.gross_r)),
        per_year=res.per_year,
        slippage_source=res.slippage_source.value,
    )


async def run_retest(  # noqa: PLR0913 - the session, the day and four keyword knobs
    session: AsyncSession,
    *,
    today: dt.date,
    force: bool = False,
    families: Sequence[str] | None = None,
    end: dt.date | None = None,
    batch: int = BOX_SYMBOL_BATCH,
) -> JsonObject:
    """Run the families (all of them by default) and append one row per family per tenant."""
    if not force and not rt.retest_due(today, await last_run(session)):
        return {"skipped": f"not due: the re-test runs once in each of {sorted(rt.RETEST_MONTHS)}"}
    chosen = rt.FAMILIES if not families else tuple(f for f in rt.FAMILIES if f.key in families)
    unknown = sorted(set(families or ()) - rt.FAMILY_KEYS)
    if unknown:
        return {"refused": f"unknown families {unknown}; known: {sorted(rt.FAMILY_KEYS)}"}
    users = await scan_users(session)
    if not users:
        return {"skipped": "no user has fo_sleeve_config rows (run fno_cli seed)"}
    stop = end or today
    futures = await load_futures(session, SAMPLE_FROM, stop)
    if futures.is_empty():
        return {"skipped": "fo_contract_daily holds no futures rows (run the backfill)"}
    measured = await measured_spreads(session)
    loop = asyncio.get_running_loop()

    def loader(symbols: frozenset[str]) -> pl.DataFrame:
        call = load_options(session, futures, symbols, SAMPLE_FROM, stop)
        return asyncio.run_coroutine_threadsafe(call, loop).result()

    def index_loader(symbol: str) -> pl.DataFrame:
        call = load_index_rows(session, futures, symbol, SAMPLE_FROM, stop)
        return asyncio.run_coroutine_threadsafe(call, loop).result()

    def compute() -> list[rt.FamilyResult]:
        cont = research.continuous(futures)
        return [
            rt.run_family(
                f, futures, loader, measured, cont=cont, batch=batch, load_index=index_loader
            )
            for f in chosen
        ]

    results = await asyncio.to_thread(compute)
    for user_id in users:
        session.add_all(_row(user_id, res) for res in results)
    await session.flush()
    return {
        "users": len(users),
        "rows": len(users) * len(results),
        "families": {
            res.family: {
                "n": res.n,
                "net_r": res.net_r,
                "gross_r": res.gross_r,
                "slippage": res.slippage_source.value,
            }
            for res in results
        },
    }
