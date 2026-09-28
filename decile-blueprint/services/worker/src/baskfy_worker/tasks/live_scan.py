"""LV8 — the TWT and VBT scans run on today's live bar, and their entries are taken now.

MAULIK'S DECISION, 28 Sep 2026 (DECISIONS-LV LV8.0), IN HIS WORDS
-----------------------------------------------------------------
*"Equities we are going to trade would be a swing trade, not an intraday trade. I think I don't
want to backtest since it is already working on the daily chart. The strategy would be the same
live. What we consider is we'll collect the live data and directly start trading on it."*

Asked with options, he chose: TWT's and VBT's Scan build today's provisional bar from Kite quotes
and run **the same detector** over today-so-far (as the swing book's SW15 scan has done since
3 Sep); a signal is entered **now, at market with Kite market protection**, the GTT stop in the
same request; TWT's auto-execute confirms such a plan at any hour of the session; VBT's is
confirmed by hand. He accepted, in the option text, that the tested entry timing changes and
that no backtest is wanted. This reverses DECISIONS-TW TW12.2 and VB12's "closed session only"
for the two Scan buttons — his reversal, recorded as TW19 and VB14.

WHAT THIS MODULE DOES
---------------------
1. Decides which session the scan is of (:func:`swing_scan_now.decide_session` — one rule for all
   three sleeves): on a trading day from 09:15 until tonight's publish, **today**, whose bar does
   not exist yet, so one is built per name in the sleeve's universe from a live quote
   (:func:`provisional_daily_bars` — open/high/low from the quote's ``ohlc``, close = last price,
   volume so far, in the adjusted space the detectors read, with the raw close beside it); before
   09:15 or after the publish, the last published session, plain.
2. Runs the sleeve's own detector over the published history plus that bar
   (``run_detect_twt`` / ``run_detect_vbt`` with ``extra_bars``), every row stamped
   ``provisional``. The TWT ratchet does not run on a provisional bar.
3. Builds a **LIVE** plan from the provisional signals with the sleeves' own planners — TWT's
   entries as ``BUY_AT_OPEN`` (the desk's confirm is already "at market, now, with protection";
   LV2.3), VBT's as ``BUY_AT_MARKET`` (LV8's new kind) — sized on the live close, expiring in
   thirty minutes like the MORNING plan. No exits are planned from a live bar: the evening's exit
   rules read a *closed* bar, and a half-day close is not one.

WHAT IT NEVER DOES
------------------
Place anything. The plan's lines are ``PROPOSED``; the desk's confirm (or, for TWT, the session
supervisor's drain under all three flags) is what turns one into an order, through the gateway.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final, Protocol

import polars as pl
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.twt_sleeve import book_state as twt_book_state
from baskfy_api.twt_sleeve import load_sleeve as twt_load_sleeve
from baskfy_api.twt_sleeve import slot_multiplier as twt_slot_multiplier
from baskfy_api.vbt_sleeve import load_sleeve as vbt_load_sleeve
from baskfy_core.models import (
    Instrument,
    TwOrder,
    TwPlan,
    TwPlanLine,
    VbConfig,
    VbPlan,
    VbPlanLine,
)
from baskfy_core.models.base import JsonObject
from baskfy_core.twt import plan as twt_plan
from baskfy_core.vbt import plan as vbt_plan
from baskfy_core.vbt.orders import LIVE_STATES
from baskfy_core.vbt.sizing import first_live_multiplier
from baskfy_providers.records import QuoteRecord
from baskfy_worker.steps import StepOutcome
from baskfy_worker.tasks import twt_evening, vbt_evening
from baskfy_worker.tasks.swing_premarket import QuoteSource
from baskfy_worker.tasks.swing_scan_now import ScanDecision, decide_session, last_factors
from baskfy_worker.tasks.twt import BAR_SCHEMA as TWT_BAR_SCHEMA
from baskfy_worker.tasks.twt import load_twt_config, run_detect_twt
from baskfy_worker.tasks.twt import load_universe as twt_universe
from baskfy_worker.tasks.vbt import BAR_SCHEMA as VBT_BAR_SCHEMA
from baskfy_worker.tasks.vbt import load_universe as vbt_universe
from baskfy_worker.tasks.vbt import load_vbt_config, run_detect_vbt

log = logging.getLogger(__name__)

SOURCE_LIVE: Final = "LIVE"
IST: Final = dt.timezone(dt.timedelta(hours=5, minutes=30), name="IST")

__all__ = [
    "IN_FLIGHT_LINE_STATES",
    "IST",
    "SOURCE_LIVE",
    "BarBuild",
    "LiveName",
    "LiveReport",
    "LiveScan",
    "PlanBuild",
    "build_live_plan_twt",
    "build_live_plan_vbt",
    "decide_session",
    "live_frame",
    "live_names",
    "provisional_daily_bars",
    "run_twt_live",
    "run_vbt_live",
]


@dataclass(frozen=True, slots=True)
class LiveName:
    """One name in a sleeve's universe, as the quote read needs it."""

    instrument_id: int
    symbol: str


@dataclass(slots=True)
class BarBuild:
    """What became of the quotes on the way to a bar each."""

    frame: pl.DataFrame
    quotes: int = 0
    skipped: dict[str, int] = field(
        default_factory=lambda: {"no_quote": 0, "no_factor": 0, "bad_price": 0}
    )

    def as_detail(self) -> dict[str, object]:
        return {
            "quotes": self.quotes,
            "bars_built": self.frame.height,
            "skipped": dict(self.skipped),
        }


def _money(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def provisional_daily_bars(
    names: Sequence[LiveName],
    quotes: Sequence[QuoteRecord],
    *,
    factors: dict[int, float],
    on: dt.date,
    schema: pl.Schema,
) -> BarBuild:
    """One bar per name that answered, in the sleeve's own bar schema.

    The quote is an exchange print; the published series is ``raw x adj_factor``, so every price
    is multiplied by the name's last factor (the same rule SW15 applies), with the raw last price
    kept as ``close_raw`` for the rules that read the exchange's number (TWT ``04`` §3.1, VBT's
    ``Close > 30``). A quote with volume 0 is still a bar; one with no ``ohlc`` side takes the
    last price for it; a name with no factor (its last bar is not the last session) is skipped
    and counted, never invented.
    """
    by_symbol = {name.symbol: name for name in names}
    build = BarBuild(frame=pl.DataFrame(schema=schema), quotes=len(quotes))
    rows: list[dict[str, object]] = []
    answered: set[str] = set()
    for quote in quotes:
        name = by_symbol.get(quote.symbol)
        if name is None:
            continue
        answered.add(quote.symbol)
        factor = factors.get(name.instrument_id)
        if factor is None or factor <= 0:
            build.skipped["no_factor"] += 1
            continue
        last = _money(quote.last_price)
        if last is None or last <= 0:
            build.skipped["bad_price"] += 1
            continue
        open_ = _money(quote.open) or last
        high = max(_money(quote.high) or last, last)
        low = min(_money(quote.low) or last, last)
        circuit = _money(quote.upper_circuit)
        row: dict[str, object] = {
            "instrument_id": name.instrument_id,
            "symbol": name.symbol,
            "date": on,
            "open": open_ * factor,
            "high": high * factor,
            "low": low * factor,
            "close": last * factor,
            "close_raw": last,
            "volume": float(max(int(quote.volume), 0)),
            "upper_circuit": None if circuit is None else circuit * factor,
            "adj_factor": factor,
        }
        if "is_etf" in schema.names():
            row["is_etf"] = False
        rows.append(row)
    build.skipped["no_quote"] = len(by_symbol) - len(answered)
    if rows:
        build.frame = pl.DataFrame(rows, schema=schema)
    return build


async def live_names(session: AsyncSession, instrument_ids: set[int]) -> list[LiveName]:
    if not instrument_ids:
        return []
    rows = (
        await session.execute(
            select(Instrument.id, Instrument.symbol).where(Instrument.id.in_(list(instrument_ids)))
        )
    ).all()
    return sorted((LiveName(int(i), str(s)) for i, s in rows), key=lambda n: n.symbol)


async def live_frame(
    session: AsyncSession,
    *,
    names: Sequence[LiveName],
    decision: ScanDecision,
    quotes: QuoteSource,
    schema: pl.Schema,
) -> BarBuild:
    """The sleeve's universe as of the last published session, quoted, one bar each for today."""
    if not names:
        return BarBuild(frame=pl.DataFrame(schema=schema))
    factors = await last_factors(
        session, on=decision.published_as_of, instrument_ids=[n.instrument_id for n in names]
    )
    records = quotes.quotes([name.symbol for name in names])
    return provisional_daily_bars(
        names, records, factors=factors, on=decision.session_date, schema=schema
    )


class LiveScan(Protocol):
    """The provisional path as the two scan tasks call it. Production binds
    :func:`run_twt_live` / :func:`run_vbt_live` with the sleeve's ``execution_enabled`` through
    ``functools.partial`` so the scan modules never name the flag."""

    async def __call__(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        decision: ScanDecision,
        quotes: QuoteSource,
        now: dt.datetime,
    ) -> LiveReport: ...


#: A plan line in one of these states has been, or is being, sent. A LIVE plan carrying one is
#: **kept** on a re-scan rather than rebuilt: ``store_plan`` replaces the day's plan of a source
#: wholesale, and replacing a plan whose buy is at the broker would lose the line the order came
#: from and offer the same name again under a new ``client_id``.
IN_FLIGHT_LINE_STATES: Final[frozenset[str]] = frozenset({"CONFIRMED", "SENT", "FILLED"})

#: ``tw_order`` states that mean the name has been bought, or is being bought, today. TWT's
#: ``BookState`` knows positions only (``03`` §6: no working orders in this sleeve), so a live
#: scan removes these names from its candidates itself — a market buy sent thirty seconds ago is
#: not yet a position, and a second plan line for it would be a second buy.
ORDERED_TODAY_STATES: Final[frozenset[str]] = frozenset({"CONFIRMED", "SENT", "PARTIAL", "FILLED"})


@dataclass(frozen=True, slots=True)
class PlanBuild:
    """What a live plan build came to."""

    plan_id: str | None
    entries: int
    #: True when today's LIVE plan was left as it was because a line of it is at the broker.
    kept: bool = False
    #: Names dropped from the candidates because an order for them exists today.
    already_ordered: int = 0


async def _in_flight_live_plan(
    session: AsyncSession,
    *,
    plan_model: type[TwPlan] | type[VbPlan],
    line_model: type[TwPlanLine] | type[VbPlanLine],
    user_id: int,
    trade_date: dt.date,
) -> str | None:
    """Today's LIVE plan's ``plan_id`` if any of its lines is in flight, else ``None``."""
    row = (
        await session.execute(
            select(plan_model.plan_id)
            .join(line_model, line_model.plan_id == plan_model.id)
            .where(
                plan_model.user_id == user_id,
                plan_model.session_date == trade_date,
                plan_model.source == SOURCE_LIVE,
                line_model.state.in_(sorted(IN_FLIGHT_LINE_STATES)),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return None if row is None else str(row)


@dataclass(frozen=True, slots=True)
class LiveReport:
    """What one live scan came to — the detection and, when there were signals, the plan."""

    session_date: dt.date
    provisional: bool
    signals: int
    plan_id: str | None
    entries: int
    detail: JsonObject

    def as_detail(self) -> JsonObject:
        return {
            "session_date": self.session_date.isoformat(),
            "provisional": self.provisional,
            "signals": self.signals,
            "plan_id": self.plan_id,
            "entries": self.entries,
            **self.detail,
        }


# --- TWT -----------------------------------------------------------------------------------------


async def build_live_plan_twt(
    session: AsyncSession,
    *,
    user_id: int,
    trade_date: dt.date,
    execution_enabled: bool,
    now: dt.datetime,
) -> PlanBuild:
    """A LIVE plan from today's provisional TWT signals: entries only, sized on the live close.

    The same parts the evening and the MORNING rebuild use — the gate, the sleeve's money, the
    book, the slot multiplier, ``build_entries``, ``assemble``, ``store_plan`` — and none of the
    evening's side effects: no exits (a half-day close is not the ratchet's input), no session
    settle, no alert. A LIVE plan with a line at the broker is **kept**, not rebuilt; a name with
    an order today is not offered again.
    """
    kept = await _in_flight_live_plan(
        session, plan_model=TwPlan, line_model=TwPlanLine, user_id=user_id, trade_date=trade_date
    )
    if kept is not None:
        return PlanBuild(kept, 0, kept=True)
    gate_row = await twt_evening.gate_for_session(session, user_id, trade_date)
    config = await load_twt_config(session, user_id)
    settings_row = await twt_evening.config_row(session, user_id)
    sleeve = await twt_load_sleeve(session, user_id, trade_date)
    equity = sleeve.value.quantize().equity_inr
    book = await twt_book_state(
        session, user_id=user_id, as_of=trade_date, signal_date=trade_date, entries_on=trade_date
    )
    multiplier = await twt_slot_multiplier(
        session, user_id=user_id, execution_enabled=execution_enabled, config=config
    )
    candidates = await twt_evening.candidates_for(session, user_id, trade_date)
    ordered = set(
        (
            await session.execute(
                select(TwOrder.instrument_id).where(
                    TwOrder.user_id == user_id,
                    TwOrder.signal_date == trade_date,
                    TwOrder.state.in_(sorted(ORDERED_TODAY_STATES)),
                )
            )
        )
        .scalars()
        .all()
    )
    offered = [c for c in candidates if c.instrument_id not in ordered]
    entries, skips = twt_plan.build_entries(
        offered,
        gate=gate_row,
        equity=equity,
        book=book,
        config=config,
        slot_multiplier=multiplier,
        max_open_positions=None if settings_row is None else settings_row.max_open_positions,
    )
    dropped = len(candidates) - len(offered)
    if not entries:
        return PlanBuild(None, 0, already_ordered=dropped)
    entries = [
        dataclasses.replace(
            line, note=f"live scan {now.astimezone(IST):%H:%M}: buy now, at market; {line.note}"
        )
        for line in entries
    ]
    plan = twt_plan.assemble(trade_date, gate_row, equity, [], entries, skips, SOURCE_LIVE)
    plan_id = await twt_evening.store_plan(
        session, user_id=user_id, as_of=trade_date, source=SOURCE_LIVE, plan=plan, now=now
    )
    return PlanBuild(str(plan_id), len(entries), already_ordered=dropped)


async def run_twt_live(  # noqa: PLR0913 - one keyword per input the scan depends on
    session: AsyncSession,
    *,
    user_id: int,
    decision: ScanDecision,
    quotes: QuoteSource,
    now: dt.datetime,
    execution_enabled: bool = False,
) -> LiveReport:
    """Today-so-far for TWT: the provisional bar, the detector, the LIVE plan."""
    config = await load_twt_config(session, user_id)
    universe = await twt_universe(session, config)
    names = await live_names(session, universe)
    build = await live_frame(
        session, names=names, decision=decision, quotes=quotes, schema=TWT_BAR_SCHEMA
    )
    outcome = StepOutcome()
    signals = await run_detect_twt(
        session,
        outcome,
        decision.session_date,
        user_id=user_id,
        extra_bars=build.frame,
        provisional=True,
    )
    built = PlanBuild(None, 0)
    if signals:
        built = await build_live_plan_twt(
            session,
            user_id=user_id,
            trade_date=decision.session_date,
            execution_enabled=execution_enabled,
            now=now,
        )
    return LiveReport(
        decision.session_date,
        True,
        signals,
        built.plan_id,
        built.entries,
        {
            **build.as_detail(),
            "plan_kept": built.kept,
            "already_ordered": built.already_ordered,
            "status": outcome.status.value,
            **dict(outcome.detail),
        },
    )


# --- VBT -----------------------------------------------------------------------------------------


async def build_live_plan_vbt(
    session: AsyncSession,
    *,
    user_id: int,
    trade_date: dt.date,
    execution_enabled: bool,
    now: dt.datetime,
) -> PlanBuild:
    """A LIVE plan from today's provisional VBT signals: ``BUY_AT_MARKET`` entries only.

    The evening's parts — the gate, the sleeve, the book, the first-live multiplier,
    ``build_entries``, ``assemble``, ``store_plan`` — without its sweep of expired limits, its
    exit queue or its session settle: those read a closed session. Each entry is the evening's
    ``PLACE_LIMIT`` line re-kinded to ``BUY_AT_MARKET`` (same size, same stop) so the desk sends
    a MARKET buy at the live price with Kite market protection. A LIVE plan with a line at the
    broker is **kept**, not rebuilt; a name with a working order is already a ``build_entries``
    refusal through ``BookState.working_instrument_ids``.
    """
    kept = await _in_flight_live_plan(
        session, plan_model=VbPlan, line_model=VbPlanLine, user_id=user_id, trade_date=trade_date
    )
    if kept is not None:
        return PlanBuild(kept, 0, kept=True)
    config = await load_vbt_config(session, user_id)
    positions = await vbt_evening.open_positions(session, user_id)
    orders = await vbt_evening.live_orders(session, user_id)
    symbols = await vbt_evening.symbols_for(
        session,
        sorted({row.instrument_id for row in positions} | {row.instrument_id for row in orders}),
    )
    sleeve = (await vbt_load_sleeve(session, user_id, trade_date)).quantize()
    gate = await vbt_evening.gate_for_session(session, user_id, trade_date)
    candidates = await vbt_evening.candidates_for(session, user_id, trade_date)
    config_row = (
        await session.execute(select(VbConfig).where(VbConfig.user_id == user_id))
    ).scalar_one_or_none()
    multiplier = first_live_multiplier(
        sessions_left=0 if config_row is None else config_row.first_live_sessions_left,
        execution_enabled=execution_enabled,
        config=config.sizing,
    )
    book = vbt_plan.BookState(
        open_instrument_ids=frozenset(row.instrument_id for row in positions),
        open_entry_counts=dict(Counter(row.instrument_id for row in positions)),
        working_instrument_ids=frozenset(
            row.instrument_id for row in orders if row.state in {s.value for s in LIVE_STATES}
        ),
        open_exposure_inr=sleeve.open_exposure_inr,
        cash_available_inr=sleeve.cash_available_inr,
        entries_already_this_session=await vbt_evening.entries_confirmed_today(
            session, user_id, trade_date
        ),  # a LIVE plan executes today
        positions_naked_of_gtt=vbt_evening.naked_positions(positions, symbols),
    )
    entries, skips = vbt_plan.build_entries(
        candidates,
        gate=gate,
        equity=sleeve.equity_inr,
        book=book,
        config=config,
        slot_multiplier=multiplier,
        max_open_positions=None if config_row is None else config_row.max_open_positions,
    )
    if not entries:
        return PlanBuild(None, 0)
    entries = [
        dataclasses.replace(
            line,
            kind=vbt_plan.LineKind.BUY_AT_MARKET,
            note=f"live scan {now.astimezone(IST):%H:%M}: buy now, at market; {line.note}",
        )
        for line in entries
    ]
    plan = vbt_plan.assemble(trade_date, gate, sleeve.equity_inr, [], entries, skips, SOURCE_LIVE)
    plan_id = await vbt_evening.store_plan(
        session, user_id=user_id, as_of=trade_date, source=SOURCE_LIVE, plan=plan, now=now
    )
    return PlanBuild(str(plan_id), len(entries))


async def run_vbt_live(  # noqa: PLR0913 - one keyword per input the scan depends on
    session: AsyncSession,
    *,
    user_id: int,
    decision: ScanDecision,
    quotes: QuoteSource,
    now: dt.datetime,
    execution_enabled: bool = False,
) -> LiveReport:
    """Today-so-far for VBT: the provisional bar, the detector, the LIVE plan."""
    config = await load_vbt_config(session, user_id)
    universe = await vbt_universe(session, config)
    names = await live_names(session, universe)
    build = await live_frame(
        session, names=names, decision=decision, quotes=quotes, schema=VBT_BAR_SCHEMA
    )
    outcome = StepOutcome()
    signals = await run_detect_vbt(
        session,
        outcome,
        decision.session_date,
        user_id=user_id,
        extra_bars=build.frame,
        provisional=True,
    )
    built = PlanBuild(None, 0)
    if signals:
        built = await build_live_plan_vbt(
            session,
            user_id=user_id,
            trade_date=decision.session_date,
            execution_enabled=execution_enabled,
            now=now,
        )
    return LiveReport(
        decision.session_date,
        True,
        signals,
        built.plan_id,
        built.entries,
        {
            **build.as_detail(),
            "plan_kept": built.kept,
            "status": outcome.status.value,
            **dict(outcome.detail),
        },
    )
