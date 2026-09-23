"""One backtest day per sleeve, by the live code (``04`` §13, ``06`` OP12).

A backtest that re-implements the sleeves would test the re-implementation. These day functions
instead run **the functions the desk and the worker run**, over a day's bars and a source of chain
snapshots:

* **Tier 1** (:func:`tier1_day`) — each sleeve's own gate and trigger (``condor.observe``,
  ``directional.day_filters`` + ``find_trigger``, ``expiry_setups.gap_hold`` / ``range_break``),
  and for O2/O3 the index's move after the trigger to the hard exit (MFE/MAE in points). No price.
* **Tier 2 / Tier 3** (:func:`priced_day`) — the plan builder (``plan.decide_o1``,
  ``plan_o2.decide_o2``, ``plan_o3.decide_o3``) minute by minute until it decides; the entry by
  ``executor.run_entry`` filled from the snapshot's depth (``execution.simulate_fill``, ``04``
  §8.4); ``exits.evaluate`` every minute on the snapshot's marks; the exit by
  ``executor.run_exit``; and the day's net ₹ and R by ``ledger.journal_figures`` with ``04`` §6's
  costs. Tier 2's snapshots are :class:`ModelChain` (Black-76 at the previous day's India VIX as a
  flat IV, forward = spot, §6.2's synthetic spread — §13.2); Tier 3's are the collector's
  (:class:`StoredChain`, §13.3).

R is the plan's own risk per lot at one lot (``04`` §9.1's paper-one-lot R), so a backtest day and
a paper day read the same.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from baskfy_core.options import condor, directional, expiry_setups
from baskfy_core.options.backtest import DayOutcome, tier2_quote
from baskfy_core.options.bars import IST, Bar, closed, ist
from baskfy_core.options.calendar import RoleReason, expiries, role
from baskfy_core.options.chain import Level, OptionQuote
from baskfy_core.options.config import OptionsCeilings, OptionsConfig, Side, Sleeve
from baskfy_core.options.execution import Attempt, LegRole, simulate_fill
from baskfy_core.options.executor import Book, Fill, Outcome, Result, run_entry, run_exit
from baskfy_core.options.exits import IndexState, LegMark, OpenLeg, OpenPosition, evaluate
from baskfy_core.options.greeks import year_fraction
from baskfy_core.options.ledger import LegFill, journal_figures
from baskfy_core.options.plan import PlanLeg, PlanState, decide_o1
from baskfy_core.options.plan_o2 import decide_o2
from baskfy_core.options.plan_o2 import wanted_minutes as o2_wanted
from baskfy_core.options.plan_o3 import decide_o3
from baskfy_core.options.plan_o3 import wanted_minutes as o3_wanted
from baskfy_core.options.scan import DayContext, MarketDay, Snapshot, SnapshotBook
from baskfy_core.options.structures import Candidate, Direction, Structure

_TICK = Decimal("0.05")
_SESSION_END = dt.time(15, 30)
_O1 = frozenset({Sleeve.O1M, Sleeve.O1W})


class SnapshotSource(Protocol):
    """A chain at a minute, or ``None`` when that minute has none."""

    def at(self, minute: dt.datetime) -> Snapshot | None: ...


@dataclass(frozen=True, slots=True)
class StoredChain:
    """Tier 3: the collector's minutes (``op_chain_snapshot``), keyed by minute."""

    minutes: Mapping[dt.datetime, Snapshot]

    def at(self, minute: dt.datetime) -> Snapshot | None:
        return self.minutes.get(_minute(minute))


@dataclass(frozen=True, slots=True)
class ModelChain:
    """Tier 2: a chain priced by Black-76 at the previous India VIX close as a flat IV, forward =
    spot (the last closed minute's close), with §6.2's synthetic spread either side and
    ``depth`` units resting at each touch (``04`` §13.2). Strikes are the master's, within
    ``strikes`` steps of spot, for the two nearest expiries — the collector's own reach."""

    market: MarketDay
    options: OptionsConfig
    strikes: int = 15
    depth: int = 1300
    rate: float = 0.065
    _cache: dict[dt.datetime, Snapshot | None] = field(default_factory=dict)

    def at(self, minute: dt.datetime) -> Snapshot | None:
        key = _minute(minute)
        if key not in self._cache:
            self._cache[key] = self._build(key)
        return self._cache[key]

    def _build(self, minute: dt.datetime) -> Snapshot | None:
        seen = closed(self.market.bars, minute + dt.timedelta(minutes=1))
        vix = self.market.vix_prev_close
        if not seen or vix is None or vix <= 0:
            return None
        spot = seen[-1].close
        day = self.market.trade_date
        nearest = [e for e in expiries(self.market.contracts, "NIFTY") if e >= day][:2]
        quotes: list[OptionQuote] = []
        for contract in self.market.contracts:
            if contract.expiry not in nearest:
                continue
            steps = abs(contract.strike - spot) / Decimal(50)
            if steps > self.strikes:
                continue
            years = max(year_fraction(minute, contract.expiry, _SESSION_END), 1e-6)
            model = tier2_quote(
                spot, contract.strike, years=years, vix_close=vix, kind=contract.option_type,
                rate=self.rate, rates=self.options.costs,
            )  # fmt: skip
            # To the paisa, toward the model: §6.2's synthetic spread is a crossing *cost*, and a
            # model has no observable liquidity, so its book must not fail §2.4 on its own rounding
            # (DECISIONS-OP OP12.1).
            bid = model.bid.quantize(Decimal("0.01"), rounding="ROUND_CEILING")
            ask = max(model.ask.quantize(Decimal("0.01"), rounding="ROUND_FLOOR"), bid)
            quotes.append(
                OptionQuote(
                    instrument_token=contract.instrument_token, expiry=contract.expiry,
                    strike=contract.strike, option_type=contract.option_type,
                    bid=bid if bid > 0 else None, ask=ask,
                    bids=(Level(bid, self.depth),) * 3 if bid > 0 else (),
                    asks=(Level(ask, self.depth),) * 3, oi=5_000_000, ts=minute,
                )
            )  # fmt: skip
        return Snapshot(ts=minute, spot=spot, quotes=tuple(quotes))


def _minute(at: dt.datetime) -> dt.datetime:
    aware = at if at.tzinfo is not None else at.replace(tzinfo=IST)
    return aware.astimezone(IST).replace(second=0, microsecond=0)


def _at(day: dt.date, clock: dt.time) -> dt.datetime:
    return dt.datetime.combine(day, clock, tzinfo=IST)


# --- Tier 1 --------------------------------------------------------------------------------------


def _move_after(
    bars: Sequence[Bar], start: dt.time, end: dt.time, direction: Direction, level: Decimal
) -> tuple[Decimal, Decimal]:
    """The index's best and worst close after ``start`` to ``end``, in the trade's direction."""
    sign = Decimal(1) if direction is Direction.UP else Decimal(-1)
    path = [sign * (b.close - level) for b in bars if start < ist(b.ts).time() <= end]
    if not path:
        return Decimal(0), Decimal(0)
    return max(*path, Decimal(0)), min(*path, Decimal(0))


def tier1_day(  # noqa: PLR0911, PLR0912 - one return per sleeve and verdict
    sleeve: Sleeve, market: MarketDay, context: DayContext, options: OptionsConfig
) -> DayOutcome | None:
    """``04`` §13.1: the gate and the trigger only. ``None`` for a day that is not the sleeve's."""
    day = market.trade_date
    verdict_role = role(day, sleeve, rows=market.contracts, event_days=context.event_days,
                        trading_day=market.trading_day)  # fmt: skip
    if not verdict_role.trades:
        if verdict_role.reason is RoleReason.EVENT_DAY:
            return DayOutcome(day, traded=False, skip_reason=RoleReason.EVENT_DAY.value)
        return None
    if not market.daily_closes:
        return DayOutcome(day, traded=False, skip_reason="NO_PREV_CLOSE")
    prev = market.daily_closes[-1]
    end = _at(day, _SESSION_END)
    grace = options.chain.stale_scan_seconds
    bars = closed(market.bars, end)
    if sleeve in _O1:
        cfg = options.condor_monthly if sleeve is Sleeve.O1M else options.condor_weekly
        gate = condor.observe(market.bars, prev, now=end, event_day=False, config=cfg,
                              grace_seconds=grace)  # fmt: skip
        if gate.reasons:
            return DayOutcome(day, traded=False, skip_reason=gate.reasons[0].value)
        return DayOutcome(day, traded=True, trigger_time=cfg.plan_time)
    if sleeve is Sleeve.O2:
        cfg2 = options.directional
        filters = directional.day_filters(market.bars, market.daily_closes, market.vix_prev_close,
                                          now=end, event_day=False, config=cfg2,
                                          grace_seconds=grace)  # fmt: skip
        if filters.reasons or filters.trend is None:
            reason = filters.reasons[0].value if filters.reasons else "TREND_UNKNOWN"
            return DayOutcome(day, traded=False, skip_reason=reason)
        trig = directional.find_trigger(
            market.bars, or_high=filters.or_high or Decimal(0), or_low=filters.or_low or Decimal(0),
            direction=filters.trend, now=end, config=cfg2,
            session_open=options.calendar.market_open, grace_seconds=grace,
        )  # fmt: skip
        if trig.trigger is None:
            return DayOutcome(day, traded=False, skip_reason="NO_TRIGGER")
        mfe, mae = _move_after(bars, trig.trigger.close_time, cfg2.hard_exit_time, filters.trend,
                               trig.trigger.close)  # fmt: skip
        return DayOutcome(day, traded=True, trigger_time=trig.trigger.close_time,
                          mfe_points=mfe, mae_points=mae)  # fmt: skip
    cfg3 = options.expiry_setups
    if sleeve is Sleeve.O3B:
        gh = expiry_setups.gap_hold(
            market.bars,
            prev,
            now=end,
            event_day=False,
            config=cfg3,
            session_open=options.calendar.market_open,
            grace_seconds=grace,
        )
        if gh.reasons:
            return DayOutcome(day, traded=False, skip_reason=gh.reasons[0].value)
        if not gh.triggered or gh.direction is None:
            return DayOutcome(day, traded=False, skip_reason="NO_TRIGGER")
        level = next((b.close for b in reversed(bars) if ist(b.ts).time() <= cfg3.o3b_hold_end),
                     prev)  # fmt: skip
        mfe, mae = _move_after(bars, cfg3.o3b_hold_end, cfg3.hard_exit_time, gh.direction, level)
        return DayOutcome(day, traded=True, trigger_time=cfg3.o3b_plan_time,
                          mfe_points=mfe, mae_points=mae)  # fmt: skip
    rb = expiry_setups.range_break(
        market.bars,
        prev,
        now=end,
        event_day=False,
        config=cfg3,
        session_open=options.calendar.market_open,
        grace_seconds=grace,
    )
    if rb.reasons:
        return DayOutcome(day, traded=False, skip_reason=rb.reasons[0].value)
    if rb.trigger_time is None or rb.direction is None or rb.trigger_close is None:
        return DayOutcome(day, traded=False, skip_reason="NO_TRIGGER")
    mfe, mae = _move_after(bars, rb.trigger_time, cfg3.hard_exit_time, rb.direction,
                           rb.trigger_close)  # fmt: skip
    return DayOutcome(day, traded=True, trigger_time=rb.trigger_time, mfe_points=mfe,
                      mae_points=mae)  # fmt: skip


# --- Tier 2 / Tier 3 -----------------------------------------------------------------------------


@dataclass
class _SnapshotVenue:
    """The executor's venue over one snapshot: the book is the snapshot's, fills walk its depth."""

    snapshot: Snapshot
    legs: Mapping[LegRole, int]
    options: OptionsConfig
    fills: list[LegFill] = field(default_factory=list)

    def _quote(self, role: LegRole) -> OptionQuote:
        token = self.legs[role]
        found = next((q for q in self.snapshot.quotes if q.instrument_token == token), None)
        if found is None:
            raise LookupError(f"token {token} not in the snapshot")
        return found

    def quote(self, role: LegRole) -> Book:
        q = self._quote(role)
        return Book(bid=q.bid or Decimal(0), ask=q.ask or Decimal(0))

    def send(self, role: LegRole, attempt: Attempt, quantity: int, closing: bool) -> Fill:
        q = self._quote(role)
        ladder = q.asks if attempt.side is Side.BUY else q.bids
        sim = simulate_fill(attempt.side, ladder, quantity, limit_price=attempt.price, tick=_TICK,
                            config=self.options.execution)  # fmt: skip
        if sim.filled and sim.avg_price is not None:
            self.fills.append(LegFill(attempt.side, sim.avg_price, sim.filled, closing))
        return Fill(sim.filled, sim.avg_price)


def _entry_points(structure: Structure, result: Result) -> Decimal:
    total = Decimal(0)
    for leg_role in result.fills:
        price = result.avg_price(leg_role) or Decimal(0)
        if structure is Structure.IRON_CONDOR:
            total += -price if leg_role.is_long else price
        else:
            total += price if leg_role.is_long else -price
    return total


@dataclass(frozen=True, slots=True)
class _Decided:
    legs: tuple[PlanLeg, ...]
    candidate: Candidate
    decision_minute: dt.datetime
    direction: Direction | None
    range_high: Decimal | None
    range_low: Decimal | None
    half_gap: Decimal | None


def _decide(  # noqa: PLR0913, PLR0917 - one return per stage; the builders' inputs
    sleeve: Sleeve,
    market: MarketDay,
    context: DayContext,
    source: SnapshotSource,
    options: OptionsConfig,
    ceilings: OptionsCeilings,
) -> _Decided | str | None:
    """The plan builder minute by minute: a decided plan, a skip reason, or None (no session)."""
    day = market.trade_date
    start = (
        dt.time(9, 30)
        if sleeve not in _O1
        else (options.condor_monthly if sleeve is Sleeve.O1M else options.condor_weekly).plan_time
    )
    minute = _at(day, start)
    while minute.time() < dt.time(14, 0):
        now = minute + dt.timedelta(seconds=45)
        if sleeve in _O1:
            cfg = options.condor_monthly if sleeve is Sleeve.O1M else options.condor_weekly
            snap = source.at(_at(day, cfg.plan_time))
            o1 = decide_o1(sleeve, market, context, snap, now, options=options, ceilings=ceilings)
            state, reasons, candidate, legs = o1.state, o1.reasons, o1.candidate, o1.legs
            as_of, direction, high, low, half = o1.as_of_minute, None, None, None, None
        elif sleeve is Sleeve.O2:
            book = SnapshotBook(
                {
                    m: source.at(m)
                    for m in o2_wanted(market, context, now, options, ceilings)
                    if m is not None
                }
            )
            o2 = decide_o2(market, context, book, now, options=options, ceilings=ceilings)
            state, reasons, candidate, legs = o2.state, o2.reasons, o2.candidate, o2.legs
            as_of, direction, high, low, half = (o2.as_of_minute, o2.direction, o2.or_high,
                                                 o2.or_low, None)  # fmt: skip
        else:
            book = SnapshotBook(
                {
                    m: source.at(m)
                    for m in o3_wanted(sleeve, market, context, now, options, ceilings)
                    if m
                }
            )
            o3 = decide_o3(sleeve, market, context, book, now, options=options, ceilings=ceilings)
            state, reasons, candidate, legs = o3.state, o3.reasons, o3.candidate, o3.legs
            as_of, direction, high, low, half = (o3.as_of_minute, o3.direction, o3.range_high,
                                                 o3.range_low, o3.half_gap)  # fmt: skip
        if state is PlanState.PLANNED and candidate is not None and as_of is not None:
            return _Decided(legs, candidate, as_of, direction, high, low, half)
        if state is PlanState.SKIPPED:
            return reasons[0] if reasons else "SKIPPED"
        if state in (PlanState.NO_SESSION, PlanState.WINDOW_CLOSED):
            return None if state is PlanState.NO_SESSION else "WINDOW_CLOSED"
        minute += dt.timedelta(minutes=1)
    return "WINDOW_CLOSED"


def priced_day(  # noqa: PLR0913 - the sleeve, the day, the user, the chain and the rules
    sleeve: Sleeve,
    market: MarketDay,
    context: DayContext,
    source: SnapshotSource,
    *,
    options: OptionsConfig,
    ceilings: OptionsCeilings,
) -> DayOutcome | None:
    """``04`` §13.2 / §13.3: the day as the desk would have traded it on ``source``'s prices."""
    day = market.trade_date
    decided = _decide(sleeve, market, context, source, options, ceilings)
    if decided is None:
        return None
    if isinstance(decided, str):
        return DayOutcome(day, traded=False, skip_reason=decided)
    candidate = decided.candidate
    quantity = candidate.lots * (candidate.lot_size or 0)
    tokens = {lg.role: lg.instrument_token for lg in decided.legs}
    entry_minute = decided.decision_minute + dt.timedelta(minutes=1)
    entry_snap = source.at(entry_minute)
    if entry_snap is None:
        return DayOutcome(day, traded=False, skip_reason="NO_ENTRY_SNAPSHOT")
    venue = _SnapshotVenue(entry_snap, tokens, options)
    entry = run_entry(venue, [lg.role for lg in decided.legs], quantity, sleeve=sleeve,
                      tick=_TICK, config=options.execution)  # fmt: skip
    if entry.outcome is not Outcome.OPEN:
        return DayOutcome(day, traded=False, skip_reason=Outcome.ABANDONED_ENTRY.value)
    fills = list(venue.fills)
    points = _entry_points(candidate.structure, entry)
    r_inr = candidate.risk_per_lot_inr or Decimal(0)
    position = OpenPosition(
        sleeve=sleeve, structure=candidate.structure,
        legs=tuple(
            OpenLeg(leg_role, Decimal(0), quantity, entry.avg_price(leg_role) or Decimal(0), token)
            for leg_role, token in tokens.items()
        ),
        entry_points=points, opened_at=entry_minute, risk_budget_inr=r_inr * candidate.lots,
        direction=decided.direction, range_high=decided.range_high, range_low=decided.range_low,
        half_gap=decided.half_gap,
        width_points=options.expiry_setups.width_points
        if candidate.structure is Structure.DEBIT_SPREAD else None,
    )  # fmt: skip
    position = _with_strikes(position, decided.legs)
    minute = entry_minute + dt.timedelta(minutes=1)
    last = _at(day, dt.time(15, 29))
    while minute <= last:
        snap = source.at(minute)
        if snap is not None:
            marks = {leg_role: LegMark(q.bid, q.ask, snap.ts)
                     for leg_role, token in tokens.items()
                     for q in snap.quotes if q.instrument_token == token}  # fmt: skip
            bars = closed(market.bars, minute)
            index = IndexState(spot=bars[-1].close if bars else None, last_tick_at=minute,
                               bars=bars)  # fmt: skip
            verdict = evaluate(position, marks, index, minute + dt.timedelta(seconds=1),
                               options=options)  # fmt: skip
            if verdict is not None:
                exit_venue = _SnapshotVenue(snap, tokens, options)
                run_exit(exit_venue, dict(entry.position), sleeve=sleeve, tick=_TICK,
                         config=options.execution)  # fmt: skip
                fills += exit_venue.fills
                figures = journal_figures(
                    fills, r_inr=r_inr * candidate.lots, quantity=quantity,
                    opened_at=entry_minute, closed_at=minute, peak_points=None,
                    trough_points=None, rates=options.costs,
                )  # fmt: skip
                return DayOutcome(
                    day, traded=True, net_pnl_inr=figures.net_pnl_inr,
                    r_multiple=figures.r_multiple, closed_reason=verdict.code,
                    trigger_time=decided.decision_minute.time(),
                )  # fmt: skip
        minute += dt.timedelta(minutes=1)
    return DayOutcome(day, traded=False, skip_reason="NO_EXIT_DATA")


def _with_strikes(position: OpenPosition, legs: Sequence[PlanLeg]) -> OpenPosition:
    strikes = {lg.role: lg.strike for lg in legs}
    return OpenPosition(
        sleeve=position.sleeve, structure=position.structure,
        legs=tuple(OpenLeg(lg.role, strikes.get(lg.role, lg.strike), lg.quantity, lg.fill_price,
                           lg.instrument_token) for lg in position.legs),
        entry_points=position.entry_points, opened_at=position.opened_at,
        risk_budget_inr=position.risk_budget_inr, direction=position.direction,
        range_high=position.range_high, range_low=position.range_low, half_gap=position.half_gap,
        width_points=position.width_points,
    )  # fmt: skip


__all__ = [
    "ModelChain",
    "SnapshotSource",
    "StoredChain",
    "priced_day",
    "tier1_day",
]
