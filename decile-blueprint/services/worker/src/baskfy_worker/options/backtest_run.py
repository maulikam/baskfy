"""A sleeve's options backtest over stored days, written to ``op_backtest_run`` (OP12).

``docs/options/06`` OP12: "``tools/options/backtest.py --sleeve --tier --from --to`` and the worker
task on the compute queue; ``op_backtest_run``; cards with caveats from the row." The computation is
``baskfy_core.options.backtest_suite.run_tier`` — the live scan, plan builders, executor, exit rules
and ledger, day by day; this module only reads the days and writes the row.

What a day is made of, all from the database:

* NIFTY 50's minute bars, its daily closes and India VIX's previous close — the same readers the
  live scan uses (``scan.load_bars`` / ``daily_levels`` / ``vix_previous_close``); a day is a
  trading day when its bars exist;
* the NIFTY master **as that day saw it**: every ``op_contract`` row with an expiry on or after the
  day, expired rows included (they are never deleted, ``03`` §1), and no expiry ``op_expiry``
  records as withdrawn. The master begins when the collector began, so a day before its first
  listed expiry has no calendar — it is counted in ``uncalendared``, never silently dropped and
  never guessed (DECISIONS-OP OP12.3);
* Tier 2's chain is ``ModelChain`` over the day; Tier 3's is every ``op_chain_snapshot`` minute of
  the day, as stored (``StoredChain``);
* Tier 1's row also carries each gate threshold's signal count at x0.75 and x1.25
  (``params_json.sensitivity``);
* the tenant's event days only — a backtest is not today's book, so no pause, session or slot of
  today leaks into a past day.

One row per run: one sleeve, one tier (``04`` §13.4), the caveats stored verbatim, the params and
the git sha beside them. Nothing here reaches a broker.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import (
    Instrument,
    OpBacktestRun,
    OpChainSnapshot,
    OpContract,
    OpEventDay,
    OpExpiry,
    OpIndexMinute,
)
from baskfy_core.models.base import JsonObject
from baskfy_core.options.backtest import BacktestResult, Tier
from baskfy_core.options.backtest_suite import run_tier, tier1_sensitivity
from baskfy_core.options.config import OptionsCeilings, OptionsConfig, Sleeve
from baskfy_core.options.replay_day import ModelChain, SnapshotSource, StoredChain
from baskfy_core.options.scan import DayContext, MarketDay, Snapshot
from baskfy_worker.options.index_bars import IST, NIFTY_50, session_bounds
from baskfy_worker.options.master import to_contract
from baskfy_worker.options.scan import (
    DAILY_HISTORY,
    NIFTY_50_SLUG,
    daily_levels,
    load_bars,
    to_quote,
    vix_previous_close,
)

TIERS: Final = {"1": Tier.SIGNALS, "2": Tier.MODELLED, "3": Tier.OBSERVED}


@dataclass(slots=True)
class BacktestReport:
    """One run's result; JSON-able for the task and the CLI."""

    sleeve: str
    tier: str
    date_from: str
    date_to: str
    trading_days: int = 0
    uncalendared: int = 0
    run_id: int | None = None
    result: JsonObject = field(default_factory=dict)

    def as_dict(self) -> JsonObject:
        return {
            "sleeve": self.sleeve,
            "tier": self.tier,
            "from": self.date_from,
            "to": self.date_to,
            "trading_days": self.trading_days,
            "uncalendared": self.uncalendared,
            "run_id": self.run_id,
            **self.result,
        }


def git_sha() -> str | None:
    """The checkout's commit, stored beside the run; ``None`` outside a git tree."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).parent,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = out.stdout.strip()
    return sha[:40] or None


async def trading_days(session: AsyncSession, start: dt.date, end: dt.date) -> list[dt.date]:
    """The days in ``[start, end]`` with stored NIFTY 50 minute bars, in order."""
    first, _ = session_bounds(start)
    _, last = session_bounds(end)
    day = func.date(func.timezone("Asia/Kolkata", OpIndexMinute.ts))
    rows = await session.execute(
        select(day)
        .join(Instrument, Instrument.id == OpIndexMinute.instrument_id)
        .where(Instrument.symbol == NIFTY_50, OpIndexMinute.ts >= first, OpIndexMinute.ts < last)
        .group_by(day)
        .order_by(day)
    )
    return [d for (d,) in rows.all()]


async def market_day(
    session: AsyncSession, day: dt.date, withdrawn: frozenset[dt.date], config: OptionsConfig
) -> MarketDay:
    """The day's market facts, the master as the day saw it."""
    rows = (
        await session.execute(
            select(OpContract).where(
                OpContract.underlying == config.calendar.underlying, OpContract.expiry >= day
            )
        )
    ).scalars()
    return MarketDay(
        trade_date=day,
        trading_day=True,
        contracts=tuple(to_contract(r) for r in rows if r.expiry not in withdrawn),
        bars=await load_bars(session, NIFTY_50, day),
        daily_closes=await daily_levels(session, NIFTY_50_SLUG, day, DAILY_HISTORY),
        vix_prev_close=await vix_previous_close(session, day),
    )


async def stored_chain(session: AsyncSession, day: dt.date) -> StoredChain:
    """Every stored ``op_chain_snapshot`` minute of ``day``, as the collector wrote it."""
    start, end = session_bounds(day)
    rows = (
        await session.execute(
            select(OpChainSnapshot)
            .where(OpChainSnapshot.ts >= start, OpChainSnapshot.ts < end)
            .order_by(OpChainSnapshot.ts)
        )
    ).scalars()
    by_minute: dict[dt.datetime, list[OpChainSnapshot]] = defaultdict(list)
    for row in rows:
        by_minute[row.ts.astimezone(IST)].append(row)
    minutes: dict[dt.datetime, Snapshot] = {}
    for ts, group in by_minute.items():
        spot = next((Decimal(r.spot) for r in group if r.spot is not None), None)
        minutes[ts] = Snapshot(ts=ts, spot=spot, quotes=tuple(to_quote(r) for r in group))
    return StoredChain(minutes)


def result_json(result: BacktestResult) -> JsonObject:
    def money(value: Decimal | None) -> str | None:
        return None if value is None else str(value)

    return {
        "sessions": result.sessions,
        "traded": result.traded,
        "skipped_by_reason": dict(result.skipped_by_reason),
        "by_year": [list(row) for row in result.by_year],
        "win_rate": money(result.win_rate),
        "expectancy_r": money(result.expectancy_r),
        "net_pnl_inr": money(result.net_pnl_inr),
        "max_drawdown_r": money(result.max_drawdown_r),
        "caveats": list(result.caveats),
    }


def options_json(options: OptionsConfig) -> JsonObject:
    """The rules the run used, as plain JSON (``str`` for every ``Decimal``, date and time)."""
    rendered = json.loads(json.dumps(asdict(options), default=str))
    if not isinstance(rendered, dict):  # pragma: no cover - asdict of a dataclass is a dict
        raise TypeError("options did not render as an object")
    return {str(key): value for key, value in rendered.items()}


def _four(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(Decimal("0.0001"))


def _two(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(Decimal("0.01"))


async def run_backtest(  # noqa: PLR0913, PLR0917 - the session, the tenant, the run and the rules
    session: AsyncSession,
    user_id: int,
    sleeve: Sleeve,
    tier: Tier,
    start: dt.date,
    end: dt.date,
    *,
    options: OptionsConfig,
    ceilings: OptionsCeilings,
    sha: str | None = None,
) -> BacktestReport:
    """Run one sleeve at one tier over ``[start, end]`` and append its ``op_backtest_run`` row."""
    if end < start:
        raise ValueError("the run ends before it starts")
    report = BacktestReport(sleeve.value, tier.value, start.isoformat(), end.isoformat())
    underlying = options.calendar.underlying
    withdrawn = frozenset(
        row.expiry_date
        for row in (
            await session.execute(select(OpExpiry).where(OpExpiry.underlying == underlying))
        ).scalars()
        if "withdrawn_on" in row.detail
    )
    events = frozenset(
        (await session.execute(select(OpEventDay.date).where(OpEventDay.user_id == user_id)))
        .scalars()
        .all()
    )
    days = await trading_days(session, start, end)
    report.trading_days = len(days)
    markets: list[MarketDay] = []
    for day in days:
        market = await market_day(session, day, withdrawn, options)
        if not market.contracts:
            report.uncalendared += 1
            continue
        markets.append(market)
    sources: dict[dt.date, SnapshotSource] = {}
    if tier is Tier.OBSERVED:
        for market in markets:
            sources[market.trade_date] = await stored_chain(session, market.trade_date)

    def source_for(market: MarketDay) -> SnapshotSource:
        if tier is Tier.MODELLED:
            return ModelChain(market, options)
        return sources[market.trade_date]

    context = DayContext(event_days=events)
    result = run_tier(
        sleeve,
        tier,
        markets,
        context=context,
        options=options,
        ceilings=ceilings,
        source_for=None if tier is Tier.SIGNALS else source_for,
    )
    params: JsonObject = {
        "trading_days": report.trading_days,
        "uncalendared": report.uncalendared,
        "options": options_json(options),
    }
    if tier is Tier.SIGNALS:
        # 06 OP12: "Tier 1 all sleeves with ±25 % sensitivity on each threshold" — on the row.
        params["sensitivity"] = [
            {"threshold": f"{s.group}.{s.field}", "factor": str(s.factor), "value": str(s.value),
             "sessions": s.sessions, "signals": s.signals}
            for s in tier1_sensitivity(sleeve, markets, context=context, options=options)
        ]  # fmt: skip
    row = OpBacktestRun(
        user_id=user_id,
        sleeve=sleeve.value,
        tier=int(tier.value),
        params_json=params,
        date_from=start,
        date_to=end,
        sessions=result.sessions,
        # A Tier 1 "traded" day is a signal with no trade; from Tier 2 up a signal is a day the
        # plan builder decided, and every one of those is either traded or a counted rejection.
        signals=result.traded,
        traded=result.traded if tier is not Tier.SIGNALS else 0,
        skipped_by_reason_json=dict(result.skipped_by_reason),
        win_rate=_four(result.win_rate),
        expectancy_r=_four(result.expectancy_r),
        net_pnl_inr=_two(result.net_pnl_inr),
        max_drawdown_r=_two(result.max_drawdown_r),
        caveats="\n".join(result.caveats),
        git_sha=sha,
    )
    session.add(row)
    await session.flush()
    report.run_id = int(row.id)
    report.result = result_json(result)
    return report


__all__ = [
    "TIERS",
    "BacktestReport",
    "git_sha",
    "market_day",
    "result_json",
    "run_backtest",
    "stored_chain",
    "trading_days",
]
