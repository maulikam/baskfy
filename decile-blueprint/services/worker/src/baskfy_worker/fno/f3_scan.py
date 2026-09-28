"""The evening scan for F3, the directional index credit spread (``04`` §11, §8; F3-4).

One ``fo_scan`` row per underlying per session, in ``04`` §8's order of states: ``OPEN_POSITION``
(one spread per underlying), ``NO_DATA`` (no next session, no index history, no 75-minute bars, a
night not ingested, no expiry or strikes), ``PAUSED`` (the book's and the sleeve's pauses),
``NO_SIGNAL`` (the daily read is NONE, or the 75-minute bar disagrees), a ``REJECTED_*`` from the
proposal, or ``CANDIDATE`` with the whole proposal in ``detail``. The intraday check (the index
against the session's open) is the monitor's at 09:20 (F3-5): a closing scan cannot make it.

Reads ``fo_index_daily`` (F3-2), ``op_index_minute`` (the 75-minute source), ``op_expiry`` and
``op_contract`` (the master) and ``fo_contract_daily`` (the settles). **No network, no order.**
"""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.fno import directional as d
from baskfy_core.fno.calendar import monthly_expiries
from baskfy_core.fno.config import (
    FnoCeilings,
    FnoConfig,
    FoSleeve,
    FoSleeveGroup,
    ScanState,
    f3_sleeve_for,
)
from baskfy_core.fno.ledger import pauses_for
from baskfy_core.models import FoContractDaily, Instrument, OpContract, OpIndexMinute
from baskfy_core.models.base import JsonObject
from baskfy_core.options.chain import strike_step
from baskfy_core.options.config import Mode, OptionType
from baskfy_worker.fno.index_daily import INDEX_SYMBOL_FOR, daily_bars
from baskfy_worker.fno.scan import Market, ScanRow, UserBook

IST: Final = dt.timezone(dt.timedelta(hours=5, minutes=30))
_CENT: Final = Decimal("0.01")
#: Calendar days of minute bars read for the 75-minute confirm (ten bars are two sessions).
_MINUTE_LOOKBACK_DAYS: Final = 10
_STEP_WINDOW: Final = 10
_INTRADAY_NOTE: Final = (
    "the intraday check (the index against the session's open) and the level are read at 09:20"
)


def _money(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _j(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    return value


async def minute_bars(
    session: AsyncSession, underlying: str, trade_date: dt.date
) -> list[d.MinuteBar]:
    """The index's minute bars up to the close of ``trade_date``, stamped in IST."""
    symbol = INDEX_SYMBOL_FOR.get(underlying)
    if symbol is None:
        return []
    ident = (
        await session.execute(
            select(Instrument.id).where(
                Instrument.symbol == symbol, Instrument.instrument_type == "INDEX"
            )
        )
    ).scalar_one_or_none()
    if ident is None:
        return []
    start = dt.datetime.combine(
        trade_date - dt.timedelta(days=_MINUTE_LOOKBACK_DAYS), dt.time(0), IST
    )
    end = dt.datetime.combine(trade_date, dt.time(23, 59), IST)
    rows = await session.execute(
        select(
            OpIndexMinute.ts,
            OpIndexMinute.open,
            OpIndexMinute.high,
            OpIndexMinute.low,
            OpIndexMinute.close,
        )
        .where(
            OpIndexMinute.instrument_id == ident, OpIndexMinute.ts >= start, OpIndexMinute.ts <= end
        )
        .order_by(OpIndexMinute.ts)
    )
    return [
        d.MinuteBar(ts.astimezone(IST).replace(tzinfo=None), o, h, lo, c)
        for ts, o, h, lo, c in rows
    ]


async def _settles(
    session: AsyncSession, underlying: str, expiry: dt.date, trade_date: dt.date
) -> dict[tuple[Decimal, OptionType], Decimal]:
    rows = await session.execute(
        select(FoContractDaily.strike, FoContractDaily.option_type, FoContractDaily.settle).where(
            FoContractDaily.trade_date == trade_date,
            FoContractDaily.symbol == underlying,
            FoContractDaily.expiry == expiry,
            FoContractDaily.instrument == "OPTIDX",
        )
    )
    out: dict[tuple[Decimal, OptionType], Decimal] = {}
    for strike, kind, settle in rows:
        if kind in ("CE", "PE") and settle is not None and settle > 0:
            out[(Decimal(strike), OptionType(kind))] = Decimal(settle)
    return out


async def _listed_strikes(session: AsyncSession, underlying: str, expiry: dt.date) -> list[Decimal]:
    rows = await session.execute(
        select(OpContract.strike).where(
            OpContract.underlying == underlying, OpContract.expiry == expiry
        )
    )
    return sorted({Decimal(s) for (s,) in rows})


def _pauses(book: UserBook, sleeve: FoSleeve) -> list[str]:
    found: list[str] = []
    if book.book_pause is not None:
        found.append(book.book_pause)
    if FoSleeveGroup.F3.value in book.sleeve_pause:
        found.append(book.sleeve_pause[FoSleeveGroup.F3.value])
    found.extend(p.message for p in pauses_for(book.auto_pauses, sleeve))
    return found


def _levels_detail(lv: d.Levels) -> JsonObject:
    return {
        "close": str(lv.close),
        "support": _j(lv.support),
        "resistance": _j(lv.resistance),
        "weekly_high": str(lv.weekly_high),
        "weekly_low": str(lv.weekly_low),
        "trend_avg": str(lv.trend_avg),
        "pivot_highs": [
            {"date": p.date.isoformat(), "price": str(p.price)} for p in lv.pivot_highs[-3:]
        ],
        "pivot_lows": [
            {"date": p.date.isoformat(), "price": str(p.price)} for p in lv.pivot_lows[-3:]
        ],
    }


def _confirm_detail(verdict: d.Confirm) -> JsonObject:
    return {
        "agrees": verdict.agrees,
        "bar_start": _j(verdict.bar_start),
        "last_close": _j(verdict.last_close),
        "average": _j(verdict.average),
        "message": verdict.message,
    }


async def f3_row(  # noqa: PLR0911, PLR0912, PLR0913, PLR0915 - one return per state of 04 §8
    session: AsyncSession,
    *,
    market: Market,
    book: UserBook,
    underlying: str,
    config: FnoConfig,
    ceilings: FnoCeilings,
) -> ScanRow:
    f3 = config.f3
    sleeve = f3_sleeve_for(underlying)
    s1 = market.next_session
    base: JsonObject = {"underlying": underlying, "next_session": _j(s1), "sleeve": sleeve.value}
    held = book.open_positions.get(sleeve.value, [])
    if held:
        position = held[0]
        return ScanRow(
            underlying,
            ScanState.OPEN_POSITION.value,
            [
                f"an F3 spread on {underlying} is open (plan {position.entry_plan_id}); one per "
                "underlying (04 §11); an add is the monitor's call in the window"
            ],
            {
                **base,
                "position_id": position.id,
                "level": _j(position.stop_price),
                "legs": position.legs,
            },
        )
    if s1 is None:
        return ScanRow(
            underlying,
            ScanState.NO_DATA.value,
            [f"the trading_day calendar has no session after {market.trade_date.isoformat()}"],
            base,
        )
    need = d.bars_needed(f3)
    bars = await daily_bars(session, underlying, end=market.trade_date, limit=need)
    if len(bars) < need or bars[-1].date != market.trade_date:
        have = bars[-1].date.isoformat() if bars else "none"
        return ScanRow(
            underlying,
            ScanState.NO_DATA.value,
            [
                f"fo_index_daily has {len(bars)} of the {need} sessions F3 needs for {underlying} "
                f"up to {market.trade_date.isoformat()} (latest {have}); run fno_cli index-daily"
            ],
            base,
        )
    lv = d.levels(bars, f3)
    if lv is None:
        return ScanRow(underlying, ScanState.NO_DATA.value, ["the levels could not be read"], base)
    base = {**base, "levels": _levels_detail(lv)}
    pauses = _pauses(book, sleeve)
    if pauses:
        return ScanRow(underlying, ScanState.PAUSED.value, pauses, base)
    daily = d.daily_direction(lv)
    if daily is d.Direction.NONE:
        return ScanRow(
            underlying,
            ScanState.NO_SIGNAL.value,
            [
                f"the daily chart gives no direction: close {lv.close} against its "
                f"{f3.trend_sessions}-session average {lv.trend_avg}, support "
                f"{lv.support or 'none'}, resistance {lv.resistance or 'none'} (04 §11)"
            ],
            {**base, "direction": daily.value},
        )
    minutes = await minute_bars(session, underlying, market.trade_date)
    bars75 = d.seventy_five_minute_bars(minutes)
    if len(bars75) < f3.confirm_bars:
        return ScanRow(
            underlying,
            ScanState.NO_DATA.value,
            [
                f"{len(bars75)} completed 75-minute bars < the {f3.confirm_bars} the confirm needs "
                f"(op_index_minute for {INDEX_SYMBOL_FOR.get(underlying)}); the daily read is "
                f"{daily.value} but unconfirmed, and an unconfirmed read is not a trade (04 §11)"
            ],
            {**base, "direction_daily": daily.value},
        )
    direction, verdict = d.direction_for(lv, bars75, f3)
    base = {**base, "direction_daily": daily.value, "confirm": _confirm_detail(verdict)}
    if direction is d.Direction.NONE:
        return ScanRow(
            underlying,
            ScanState.NO_SIGNAL.value,
            [f"the daily read is {daily.value} but the 75-minute bar disagrees: {verdict.message}"],
            {**base, "direction": direction.value},
        )
    level = d.key_level(direction, lv)
    base = {**base, "direction": direction.value, "level": _j(level)}
    if not market.ingested:
        return ScanRow(
            underlying,
            ScanState.NO_DATA.value,
            [
                f"the bhavcopy for {market.trade_date.isoformat()} is not ingested "
                f"({market.ingest_status or 'never tried'}); the spread is not priced from a guess"
            ],
            base,
        )
    listed = market.expiries.get(underlying, [])
    if underlying == "NIFTY":
        expiry = d.choose_expiry(listed, market.sessions, s1, f3.min_sessions_weekly)
        kind = "weekly"
    else:
        expiry = d.choose_expiry(
            monthly_expiries(listed), market.sessions, s1, f3.min_sessions_monthly
        )
        kind = "monthly"
    if expiry is None:
        return ScanRow(
            underlying,
            ScanState.NO_DATA.value,
            [
                f"the NFO master lists no {kind} {underlying} expiry with enough sessions "
                f"after {s1.isoformat()}"
            ],
            base,
        )
    base = {**base, "expiry": expiry.isoformat(), "expiry_kind": kind}
    settles = await _settles(session, underlying, expiry, market.trade_date)
    strikes = await _listed_strikes(session, underlying, expiry) or sorted({k for k, _ in settles})
    step = strike_step(strikes, lv.close, _STEP_WINDOW)
    if step is None:
        return ScanRow(
            underlying,
            ScanState.NO_DATA.value,
            [f"no strike grid for {underlying} {expiry.isoformat()} in the master or the bhavcopy"],
            base,
        )
    lot_size = market.expiry_lots.get((underlying, expiry))
    proposal = d.propose_spread(
        mode=Mode.PAPER,
        direction=direction,
        lv=lv,
        step=step,
        prices=settles,
        lot_size=lot_size,
        capital_inr=book.capital.get(FoSleeveGroup.F3.value, Decimal(0)),
        f3=f3,
        common=config.common,
        ceilings=ceilings,
    )
    s = proposal.strikes
    legs: list[JsonObject] = [
        {
            "entry_seq": 1,
            "role": "LONG_PUT" if s.option_type is OptionType.PE else "LONG_CALL",
            "strike": str(s.wing),
            "option_type": s.option_type.value,
            "expiry": expiry.isoformat(),
            "qty_sign": 1,
            "settle": _j(proposal.wing_price),
        },
        {
            "entry_seq": 2,
            "role": "SHORT_PUT" if s.option_type is OptionType.PE else "SHORT_CALL",
            "strike": str(s.short),
            "option_type": s.option_type.value,
            "expiry": expiry.isoformat(),
            "qty_sign": -1,
            "settle": _j(proposal.short_price),
        },
    ]
    detail: JsonObject = {
        **base,
        "entry_session": s1.isoformat(),
        "strike_step": str(step),
        "lot_size": lot_size,
        "short_strike": str(s.short),
        "wing_strike": str(s.wing),
        "option_type": s.option_type.value,
        "legs": legs,
        "credit_points": _j(proposal.credit),
        "max_loss_per_unit": _j(proposal.max_loss_per_unit),
        "max_loss_per_lot_inr": _j(proposal.max_loss_per_lot_inr),
        "decay_target_mark": _j(
            None if proposal.credit is None else d.decay_target_mark(proposal.credit, f3)
        ),
        "loss_cut_mark": _j(
            None if proposal.credit is None else d.loss_cut_mark(proposal.credit, f3)
        ),
    }
    if proposal.sizing is not None:
        detail["lots"] = proposal.sizing.lots
        detail["sizing"] = {
            "lots": proposal.sizing.lots,
            "sizing_mode": proposal.sizing.sizing_mode.value,
            "risk_budget_inr": str(_money(proposal.sizing.risk_budget_inr)),
            "max_loss_inr": str(_money(proposal.sizing.max_loss_inr)),
            "message": proposal.sizing.message,
        }
    reasons = [*proposal.reasons, _INTRADAY_NOTE]
    state = proposal.state.value if proposal.state is not None else ScanState.CANDIDATE.value
    if proposal.state is None:
        reasons.insert(
            0,
            f"{direction.value}: sell the {s.option_type.value} {s.short} "
            f"{expiry.isoformat()}, wing "
            f"{s.wing}; out at once if the index trades beyond {level} (04 §11)",
        )
    return ScanRow(
        underlying,
        state,
        reasons,
        detail,
        credit=_money(proposal.credit),
        max_loss_per_lot=_money(proposal.max_loss_per_lot_inr),
    )
