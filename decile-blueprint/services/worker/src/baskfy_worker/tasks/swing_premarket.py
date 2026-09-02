"""SW6 — the morning before the open: refresh the levels, find the gaps, rebuild the plan.

`docs/swing/01` §8: "Premarket: 5-10 minutes on the watchlist and the gap scan." `docs/swing/06`
SW6: at 08:50 IST refresh every watched name's levels from the latest bar; at 09:09 pull quotes
for the liquid universe in ≤ 500-symbol batches, run ``live_gap`` and watch every name that is
gapping on volume; then rebuild the morning plan (``source=MORNING``) so the desk page shows
what the day could be before the bell.

Three steps, three functions, one entry point (:func:`run_swing_premarket`). Each step is
idempotent — the 08:50 and 09:09 Beat entries both call the same function with a different
``stage``, and a re-run of either changes nothing that the first run wrote correctly.

WHY THE LEVELS ARE REFRESHED AT ALL
-----------------------------------
`docs/swing/03` §9: "A split between detection and the morning invalidates the level:
`swing-premarket` recomputes from the latest bar rather than trusting last night's number."
Every level SW3 writes is an exchange price for the *as-of* row's adjustment factor. If a
corporate action has landed since, the factor of the latest bar differs, and the trigger a
person is watching is the wrong price by exactly that ratio. The refresh divides the adjusted
level by today's factor, which is the same arithmetic ``to_exchange_prices`` did last night
with today's number in it. A ``MANUAL`` row keeps what the person typed (`03` §4).

WHY AN EP AT THE OPEN HAS NO STOP YET
-------------------------------------
The stop for a live gap is the opening range's low or the low of the day (`04` §7.2), and at
09:09 there is no range. The watch row therefore carries the indicative price as ``trigger``
and no ``stop_ref``; the morning plan skips it (a plan line needs a stop to size against), and
the ``SIGNAL`` plan the monitor builds when the range breaks carries the verdict's own entry and
stop. `docs/swing/DECISIONS-SW.md` SW6.2.

The flag: ``BASKFY_SWING_EP_PREMARKET_ENABLED`` gates the *quote pull* only. The level refresh
and the morning plan run on data the nightly already wrote and cost no Kite call.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final, Protocol

import polars as pl
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import OhlcvDaily
from baskfy_core.models.swing import SwConfig, SwMarketDaily, SwSetupDaily, SwWatch
from baskfy_core.swing.config import DEFAULT_SWING_CONFIG, Setup, SwingConfig
from baskfy_core.swing.indicators import liquid_expr, with_swing_indicators
from baskfy_core.swing.market import ExposureTier, MarketGate
from baskfy_core.swing.opening_range import LiveGapVerdict, live_gap
from baskfy_core.swing.plan import assemble, build_entries
from baskfy_providers.records import QuoteRecord
from baskfy_worker.steps import StepOutcome, StepStatus
from baskfy_worker.tasks.swing import (
    LOOKBACK_SESSIONS,
    load_swing_bars,
    load_swing_config,
    lookback_start,
    recent_trading_days,
)
from baskfy_worker.tasks.swing_eod import (
    held_instrument_ids,
    manage_open_positions,
    sleeve_account,
    store_plan,
    watch_items,
)

log = logging.getLogger(__name__)

#: NSE's pre-open order-collection session opens at 09:00 IST; the indicative price and the
#: matched quantity ``live_gap`` reads exist from then. ``minutes_elapsed`` for the pace is
#: measured from here, because that is when the volume started accumulating. A fact about the
#: exchange, like ``SESSION_MINUTES`` in core — not a threshold, so not a config field.
PREOPEN_START: Final = dt.time(9, 0)

#: The stages the two Beat entries run. ``LEVELS`` at 08:50 costs no Kite call; ``GAPS`` at
#: 09:09 pulls quotes (when the flag allows) and rebuilds the plan either way.
STAGE_LEVELS: Final = "LEVELS"
STAGE_GAPS: Final = "GAPS"

#: What a live gap is watched as (`06` SW6: "new `sw_watch` rows (setup EP, source
#: `DETECTOR`, catalyst empty)") and how long it stays (`04` §3: ``valid_bars`` sessions).
LIVE_GAP_SOURCE: Final = "DETECTOR"


class QuoteSource(Protocol):
    """Where the 09:09 quotes come from. Production: ``KiteProvider.quotes``; tests: a fake."""

    def quotes(self, symbols: Sequence[str]) -> list[QuoteRecord]: ...


@dataclass
class PremarketReport:
    """What the morning did — the step payload and the desk page's summary line."""

    session_date: dt.date
    stage: str
    levels_refreshed: int = 0
    levels_unchanged: int = 0
    quotes_pulled: int = 0
    universe: int = 0
    gap_candidates: list[str] = field(default_factory=list)
    gaps_added: int = 0
    gaps_already_watched: int = 0
    plan_id: str | None = None
    entry_lines: int = 0
    exit_lines: int = 0
    skips: int = 0
    skipped_reason: str | None = None

    def as_detail(self) -> dict[str, object]:
        return {
            "session_date": self.session_date.isoformat(),
            "stage": self.stage,
            "levels": {"refreshed": self.levels_refreshed, "unchanged": self.levels_unchanged},
            "gaps": {
                "universe": self.universe,
                "quotes": self.quotes_pulled,
                "candidates": list(self.gap_candidates),
                "added": self.gaps_added,
                "already_watched": self.gaps_already_watched,
            },
            "plan": {
                "plan_id": self.plan_id,
                "entries": self.entry_lines,
                "exits": self.exit_lines,
                "skips": self.skips,
            },
            "skipped_reason": self.skipped_reason,
        }


# --- 08:50: the levels ---------------------------------------------------------------


async def _latest_adj_factors(
    session: AsyncSession, instrument_ids: Sequence[int], *, on: dt.date
) -> dict[int, tuple[dt.date, Decimal]]:
    """``instrument_id → (date, adj_factor)`` of the latest bar at or before ``on``."""
    if not instrument_ids:
        return {}
    rows = await session.execute(
        select(OhlcvDaily.instrument_id, OhlcvDaily.date, OhlcvDaily.adj_factor)
        .where(OhlcvDaily.instrument_id.in_(list(instrument_ids)), OhlcvDaily.date <= on)
        .order_by(OhlcvDaily.instrument_id, OhlcvDaily.date.desc())
    )
    latest: dict[int, tuple[dt.date, Decimal]] = {}
    for instrument_id, date, factor in rows:
        key = int(instrument_id)
        if key not in latest:
            latest[key] = (date, Decimal(str(factor)) if factor is not None else Decimal(1))
    return latest


def _rescale(level: Decimal | None, *, was: Decimal, now: Decimal) -> Decimal | None:
    """An exchange price written under factor ``was``, re-expressed under factor ``now``."""
    if level is None or was <= 0 or now <= 0 or was == now:
        return level
    return (level * was / now).quantize(Decimal("0.01"))


async def refresh_levels(session: AsyncSession, *, user_id: int, on: dt.date) -> tuple[int, int]:
    """Re-express every ``DETECTOR`` watch row's levels under the latest bar's adjustment factor.

    Returns ``(refreshed, unchanged)``. A row whose detection row cannot be found (the setup
    table was pruned, or the watch predates the schema) is left alone and counted as unchanged:
    a level that cannot be recomputed is still the level that was written, and blanking it would
    drop the name from the plan for a reason nobody asked for.
    """
    watching = (
        (
            await session.execute(
                select(SwWatch).where(
                    SwWatch.user_id == user_id,
                    SwWatch.state == "WATCHING",
                    SwWatch.source != "MANUAL",
                )
            )
        )
        .scalars()
        .all()
    )
    if not watching:
        return 0, 0
    factors = await _latest_adj_factors(session, [row.instrument_id for row in watching], on=on)
    refreshed = unchanged = 0
    for row in watching:
        detected = None
        if row.setup_daily_date is not None:
            detected = (
                await session.execute(
                    select(SwSetupDaily).where(
                        SwSetupDaily.user_id == user_id,
                        SwSetupDaily.instrument_id == row.instrument_id,
                        SwSetupDaily.setup == row.setup,
                        SwSetupDaily.date == row.setup_daily_date,
                    )
                )
            ).scalar_one_or_none()
        latest = factors.get(row.instrument_id)
        if detected is None or latest is None or detected.adj_factor is None:
            unchanged += 1
            continue
        _, factor_now = latest
        factor_was = Decimal(str(detected.adj_factor))
        trigger = _rescale(detected.trigger, was=factor_was, now=factor_now)
        stop_ref = _rescale(detected.stop_ref, was=factor_was, now=factor_now)
        if trigger == row.trigger and stop_ref == row.stop_ref:
            unchanged += 1
            continue
        row.trigger = trigger
        row.stop_ref = stop_ref
        refreshed += 1
    await session.flush()
    return refreshed, unchanged


# --- 09:09: the gaps -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LiquidName:
    """One name the gap scan may quote: its symbol and the volume an average day carries."""

    instrument_id: int
    symbol: str
    avg_daily_volume: Decimal
    prev_close: Decimal


async def liquid_universe(
    session: AsyncSession, *, as_of: dt.date, config: SwingConfig
) -> list[LiquidName]:
    """The names `04` §1 admits as of the last close — the ones worth a quote at the open.

    The same predicate the detectors apply (``liquid_expr``), over the same bars, so "the liquid
    universe" means one thing in the evening and the morning. ``avg_daily_volume`` is the
    ``vol_avg_rvol`` window (`EpConfig.rvol_bars`, excluding the last bar) — the denominator
    the EP detector's own rvol uses. ``prev_close`` is the last bar's exchange print
    (``close / adj_factor``), the fallback when a quote carries no previous close of its own.
    """
    start = await lookback_start(session, as_of, LOOKBACK_SESSIONS)
    bars = await load_swing_bars(session, start, as_of)
    if bars.is_empty():
        return []
    frame = (
        with_swing_indicators(bars, config)
        .group_by("instrument_id", maintain_order=True)
        .agg(
            liquid_expr(config).last().alias("liquid"),
            pl.col("symbol").last(),
            pl.col("vol_avg_rvol").last(),
            pl.col("close").last(),
            pl.col("adj_factor").last(),
            pl.col("date").last(),
        )
        .filter(pl.col("liquid") & (pl.col("date") == as_of))
        .sort("symbol")
    )
    names: list[LiquidName] = []
    for row in frame.iter_rows(named=True):
        if row["vol_avg_rvol"] is None or row["close"] is None:
            continue
        factor = row["adj_factor"] if row["adj_factor"] else 1.0
        names.append(
            LiquidName(
                instrument_id=int(row["instrument_id"]),
                symbol=str(row["symbol"]),
                avg_daily_volume=Decimal(str(row["vol_avg_rvol"])),
                prev_close=Decimal(str(round(row["close"] / factor, 2))),
            )
        )
    return names


def minutes_since_preopen(now: dt.time) -> int:
    """Minutes the pre-open volume has had to accumulate; never below one.

    ``live_gap`` pro-rates an average day's volume by ``minutes_elapsed / 375`` and answers
    "not a candidate" for zero minutes — so a scan that runs exactly at 09:00 would find nothing
    for the wrong reason. `docs/swing/DECISIONS-SW.md` SW6.1.
    """
    elapsed = (now.hour * 60 + now.minute) - (PREOPEN_START.hour * 60 + PREOPEN_START.minute)
    return max(elapsed, 1)


def evaluate_gaps(
    names: Sequence[LiquidName],
    quotes: Sequence[QuoteRecord],
    *,
    at: dt.time,
    config: SwingConfig,
) -> list[tuple[LiquidName, QuoteRecord, LiveGapVerdict]]:
    """Every name whose quote says it is gapping on volume — `04` §7.3, pure over its inputs.

    The previous close is the exchange's own (``QuoteRecord.prev_close``) when the quote carries
    it, and the last bar's raw close otherwise: a corporate action between the two would show
    up as a gap that is not one, and the exchange's number already has it applied.
    """
    by_symbol = {quote.symbol: quote for quote in quotes}
    minutes = minutes_since_preopen(at)
    found: list[tuple[LiquidName, QuoteRecord, LiveGapVerdict]] = []
    for name in names:
        quote = by_symbol.get(name.symbol)
        if quote is None:
            continue
        prev_close = quote.prev_close if quote.prev_close else name.prev_close
        verdict = live_gap(
            prev_close=prev_close,
            last_price=quote.last_price,
            volume_so_far=quote.volume,
            avg_daily_volume=name.avg_daily_volume,
            minutes_elapsed=minutes,
            config=config.opening_range,
        )
        if verdict.is_candidate:
            found.append((name, quote, verdict))
    return found


async def watch_live_gaps(  # noqa: PLR0913 - one keyword per input the scan depends on
    session: AsyncSession,
    *,
    user_id: int,
    on: dt.date,
    names: Sequence[LiquidName],
    quotes: Sequence[QuoteRecord],
    at: dt.time,
    config: SwingConfig,
) -> tuple[list[str], int, int]:
    """Write a watch row for every live gap that is not already watched.

    Returns ``(candidate symbols, added, already watched)``. A name on the list already —
    yesterday's EP, a flag the person is watching — is not duplicated: the monitor watches the
    row it has, and a second row for the same name would raise the same signal twice.
    """
    found = evaluate_gaps(names, quotes, at=at, config=config)
    if not found:
        return [], 0, 0
    watched = set(
        (
            await session.execute(
                select(SwWatch.instrument_id).where(
                    SwWatch.user_id == user_id, SwWatch.state == "WATCHING"
                )
            )
        ).scalars()
    )
    expiry = await _sessions_ahead(session, on, config.ep.valid_bars)
    added = already = 0
    for name, quote, verdict in found:
        if name.instrument_id in watched:
            already += 1
            continue
        session.add(
            SwWatch(
                user_id=user_id,
                instrument_id=name.instrument_id,
                setup=Setup.EP.value,
                source=LIVE_GAP_SOURCE,
                added_on=on,
                expires_on=expiry,
                trigger=quote.last_price,
                stop_ref=None,
                setup_daily_date=None,
                note=f"live gap {verdict.gap_pct}% on {verdict.volume_pace}x pace at {at:%H:%M}",
                catalyst=None,
                state="WATCHING",
            )
        )
        watched.add(name.instrument_id)
        added += 1
    await session.flush()
    return [name.symbol for name, _, _ in found], added, already


async def _sessions_ahead(session: AsyncSession, on: dt.date, count: int) -> dt.date:
    """The trading day ``count`` sessions after ``on`` — an EP's expiry (`04` §3)."""
    from baskfy_core.models import TradingDay  # noqa: PLC0415 - one query needs it
    from baskfy_core.seed_data import NSE_EXCHANGE_ID  # noqa: PLC0415

    rows = await session.execute(
        select(TradingDay.date)
        .where(
            TradingDay.exchange_id == NSE_EXCHANGE_ID,
            TradingDay.is_trading_day.is_(True),
            TradingDay.date > on,
        )
        .order_by(TradingDay.date)
        .limit(count)
    )
    days: list[dt.date] = [row[0] for row in rows]
    if len(days) < count:
        # The calendar runs out before the expiry does: fall back to calendar days rather than
        # never expiring, which is what a NULL would mean for a DETECTOR row.
        return on + dt.timedelta(days=count + 2)
    return days[-1]


# --- the morning plan ----------------------------------------------------------------


async def build_morning_plan(  # noqa: PLR0913 - one keyword per input the plan depends on
    session: AsyncSession,
    report: PremarketReport,
    *,
    user_id: int,
    on: dt.date,
    last_session: dt.date,
    config: SwingConfig,
    now: dt.datetime,
) -> str | None:
    """Rebuild the plan from the same watchlist as the evening's preview, ``source=MORNING``.

    The gate and the rung are the last close's (`docs/swing/03` §6: "the morning plan is rebuilt
    at 09:10 from the same watchlist"); a morning job that invented a gate from pre-open quotes
    would be planning against a tape nobody measured. ``None`` when there is no market row for
    the last session — the detectors have not run, and the report says so.
    """
    market = (
        await session.execute(
            select(SwMarketDaily).where(
                SwMarketDaily.user_id == user_id, SwMarketDaily.date == last_session
            )
        )
    ).scalar_one_or_none()
    if market is None:
        report.skipped_reason = (
            f"no sw_market_daily row for {last_session.isoformat()}; the detectors have not run"
        )
        return None
    gate = MarketGate(market.gate)
    tier = ExposureTier(
        level=market.exposure_level,
        max_open_positions=market.max_open_positions,
        max_exposure_pct=float(market.max_exposure_pct),
        new_entries_allowed=market.new_entries_allowed,
    )
    # The exits are the evening's, re-derived from the same bar: `manage` is idempotent (a stop
    # never falls, `04` §6.5), so running it again in the morning changes nothing it already did.
    exits, _, _ = await manage_open_positions(
        session, user_id=user_id, on=last_session, config=config
    )
    items, instrument_ids = await watch_items(session, user_id=user_id, on=last_session)
    config_row = await load_config_row(session, user_id)
    account = await sleeve_account(session, user_id=user_id, config_row=config_row)
    entries, skipped = build_entries(
        as_of=on, watch=items, account=account, gate=gate, tier=tier, config=config
    )
    plan = assemble(as_of=on, gate=gate, tier=tier, entries=entries, exits=exits, skipped=skipped)
    instrument_ids.update(await held_instrument_ids(session, user_id=user_id))
    report.entry_lines = len(entries)
    report.exit_lines = len(exits)
    report.skips = len(skipped)
    return await store_plan(
        session, plan, user_id=user_id, source="MORNING", instrument_ids=instrument_ids, now=now
    )


async def load_config_row(session: AsyncSession, user_id: int) -> SwConfig | None:
    row: SwConfig | None = (
        await session.execute(select(SwConfig).where(SwConfig.user_id == user_id))
    ).scalar_one_or_none()
    return row


# --- the entry point -----------------------------------------------------------------


async def run_swing_premarket(  # noqa: PLR0913 - one keyword per input the morning depends on
    session: AsyncSession,
    outcome: StepOutcome,
    session_date: dt.date,
    *,
    user_id: int,
    stage: str = STAGE_GAPS,
    ep_premarket_enabled: bool = False,
    quotes: QuoteSource | None = None,
    now: dt.datetime | None = None,
    config: SwingConfig | None = None,
) -> PremarketReport:
    """The morning's job. ``LEVELS`` refreshes; ``GAPS`` scans (flag permitting) and plans.

    ``now`` is the wall clock in IST as a naive datetime, injectable so the tests can put the
    scan at 09:09 without waiting for it. The quote source is injected for the same reason, and
    because a job that built its own broker client would be a job the tests cannot run.
    """
    stamp = now or dt.datetime.now()
    report = PremarketReport(session_date=session_date, stage=stage)
    resolved = config or await load_swing_config(session, user_id) or DEFAULT_SWING_CONFIG

    sessions = await recent_trading_days(session, session_date - dt.timedelta(days=1), 1)
    if not sessions:
        outcome.status = StepStatus.SKIPPED
        report.skipped_reason = "no trading day before the session date in the calendar"
        outcome.note(**report.as_detail())
        return report
    last_session = sessions[-1]

    report.levels_refreshed, report.levels_unchanged = await refresh_levels(
        session, user_id=user_id, on=last_session
    )
    if stage == STAGE_LEVELS:
        outcome.note(**report.as_detail())
        return report

    if ep_premarket_enabled and quotes is not None:
        names = await liquid_universe(session, as_of=last_session, config=resolved)
        report.universe = len(names)
        pulled = quotes.quotes([name.symbol for name in names]) if names else []
        report.quotes_pulled = len(pulled)
        (
            report.gap_candidates,
            report.gaps_added,
            report.gaps_already_watched,
        ) = await watch_live_gaps(
            session,
            user_id=user_id,
            on=session_date,
            names=names,
            quotes=pulled,
            at=stamp.time(),
            config=resolved,
        )
    elif ep_premarket_enabled:
        log.warning("BASKFY_SWING_EP_PREMARKET_ENABLED is on but no quote source was wired")

    report.plan_id = await build_morning_plan(
        session,
        report,
        user_id=user_id,
        on=session_date,
        last_session=last_session,
        config=resolved,
        now=stamp.astimezone(dt.UTC) if stamp.tzinfo else stamp.replace(tzinfo=IST_OFFSET),
    )
    if report.plan_id is None:
        outcome.status = StepStatus.SKIPPED
    outcome.rows_out = report.entry_lines + report.exit_lines
    outcome.note(**report.as_detail())
    return report


#: IST as a fixed offset, for stamping the plan's `built_at` when the caller passed a naive
#: IST clock. `baskfy_providers.tokens.IST` is the same zone; a fixed offset avoids importing
#: a token module for a timezone.
IST_OFFSET: Final = dt.timezone(dt.timedelta(hours=5, minutes=30))


__all__ = [
    "LIVE_GAP_SOURCE",
    "PREOPEN_START",
    "STAGE_GAPS",
    "STAGE_LEVELS",
    "LiquidName",
    "PremarketReport",
    "QuoteSource",
    "build_morning_plan",
    "evaluate_gaps",
    "liquid_universe",
    "minutes_since_preopen",
    "refresh_levels",
    "run_swing_premarket",
    "watch_live_gaps",
]
