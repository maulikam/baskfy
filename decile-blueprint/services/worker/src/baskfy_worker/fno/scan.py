"""The nightly scan: ``fo_scan`` for the next session, per user (``docs/fno/06`` FO4).

After each ingested night (``run_night`` chains it) and by hand (``fno_cli scan --date``), for
every user with ``fo_sleeve_config`` rows. **Database only, no network.** ``trade_date`` is the
session whose bhavcopy was read; every row describes the session after it. Idempotent per
``(user_id, sleeve, trade_date, symbol)`` (house rule 7): the rows are upserted on that key and a
row the re-run no longer produces is deleted, so a re-run over the same data leaves the same set.
The arithmetic is ``baskfy_core.fno.scan``'s; this module reads, decides the state (``04`` §8)
and writes. Every row carries its reasons in words, never a blank.

F1 (``F1N`` NIFTY, ``F1B`` BANKNIFTY), one row per underlying, first match wins:
``OPEN_POSITION`` → ``NO_DATA`` (no monthly in the NFO master, or the calendar does not reach its
entry session) → ``PAUSED`` (``04`` §7, a sleeve or book pause) → ``NOT_ENTRY_DAY`` (with the
next entry date) → on the entry session ``NO_DATA`` (no bhavcopy, future or ATM IV) →
``SKIPPED_EVENT`` (an ``op_event_day`` inside entry → hard exit; the proposal is still written so
Maulik can accept it on the plan) → the proposal's ``REJECTED_*`` → ``CANDIDATE``.

F2, per F&O stock in the universe plus each open position: ``OPEN_POSITION`` → ``BLOCKED_BAN``
→ ``NO_SIGNAL`` (inside the corporate-action exclusion, or no breakout/trend) → ``NO_DATA`` (the
window is too short to answer) → ``BLOCKED_REGIME`` → ``PAUSED`` → ``NO_DATA`` (no contract or
ATR) → ``REJECTED_SIZE`` (with capital only) → ``BLOCKED_CAPACITY`` → ``CANDIDATE``. When the
sleeve cannot be evaluated at all one row with symbol ``*`` says why (FO4.3).

``PAUSED`` reads ``04`` §7 through ``baskfy_core.fno.ledger.evaluate_pauses`` over the **paper**
journal only (``ledger.ENTRIES_SIMULATED``: every entry is paper while FO7.1 stands) — paper and
live are never pooled (DECISIONS-FO FO10.1, correcting FO4.9) — plus the stored pauses that bind
this mode (a hand-set one binds both).
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

import polars as pl
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.fno.calendar import (
    f2_contract_expiry,
    hard_exit_session,
    monthly_expiries,
    next_entry,
)
from baskfy_core.fno.condor import ENTRY_SEQUENCE, option_type_of, sign_of
from baskfy_core.fno.config import (
    DEFAULT_FNO_CEILINGS,
    DEFAULT_FNO_CONFIG,
    FnoCeilings,
    FnoConfig,
    FoSleeve,
    ScanState,
    f1_sleeve_for,
)
from baskfy_core.fno.ledger import FoPause, column_pause_applies, evaluate_pauses, pauses_for
from baskfy_core.fno.scan import (
    REGIME_UNDERLYING,
    CondorProposal,
    FutureProposal,
    LegPrint,
    allocate_capacity,
    f2_signals,
    propose_condor,
    propose_future,
)
from baskfy_core.fno.series import FUTURES_COLUMNS
from baskfy_core.fno.vol import OptionPrint, atm_iv
from baskfy_core.models import (
    FoBookConfig,
    FoContractDaily,
    FoIngestDay,
    FoPosition,
    FoScan,
    FoSleeveConfig,
    IndexDef,
    IndexMemberDaily,
    Instrument,
    OpContract,
    OpEventDay,
    OpExpiry,
)
from baskfy_core.models.base import JsonObject
from baskfy_core.options.config import CostRates, OptionType
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_core.universes import SECTOR_INDEX_SLUGS
from baskfy_worker.fno.ingest import STATUS_INGESTED
from baskfy_worker.fno.ledger import ENTRIES_SIMULATED, load_lifts, load_trades
from baskfy_worker.fno.underlying import trading_days

log = logging.getLogger("baskfy_worker.fno.scan")

#: Calendar days of futures history the F2 window reads: ~100 sessions, so the 50-session average
#: and the prior 20-session high are answered after a few holidays and a CA exclusion.
SCAN_LOOKBACK_DAYS: Final = 150
#: Calendar days of exchange calendar ahead of ``trade_date``: the next monthly's entry session
#: and its expiry can sit ~2 months out.
CALENDAR_AHEAD_DAYS: Final = 120
#: The sleeve-level row's symbol, when a sleeve cannot be evaluated at all (FO4.3).
SLEEVE_ROW: Final = "*"

_FUTURES: Final = ("FUTSTK", "FUTIDX")
_OPTIONS: Final = ("OPTSTK", "OPTIDX")
_CENT: Final = Decimal("0.01")
_IV_RV_NOTE: Final = "IV ÷ RV20 is recorded, not used: F1 has no IV filter (01 §1)"
_SPREAD_NOTE: Final = "the live spread (≤ 5 % of mid) and the broker margin are checked at 09:20"


# --- rows -----------------------------------------------------------------------------------------


@dataclass(slots=True)
class ScanRow:
    symbol: str
    state: str
    reasons: list[str]
    detail: JsonObject = field(default_factory=dict)
    credit: Decimal | None = None
    max_loss_per_lot: Decimal | None = None
    cost_share: Decimal | None = None
    iv: Decimal | None = None
    rv20: Decimal | None = None
    iv_rv: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.reasons or not all(r.strip() for r in self.reasons):
            raise ValueError(f"{self.symbol} {self.state}: every scan row carries its reason")


def _q(value: float | Decimal | None, places: str) -> Decimal | None:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return None
    return Decimal(repr(value) if isinstance(value, float) else value).quantize(
        Decimal(places), rounding=ROUND_HALF_UP
    )


def _money(value: float | Decimal | None) -> Decimal | None:
    return _q(value, "0.01")


def _j(value: object) -> object:
    """A JSON-safe copy: money as strings (exact), dates ISO, floats as they are."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _j(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_j(v) for v in value]
    return value


# --- market facts, shared by every user -----------------------------------------------------------


@dataclass(slots=True)
class Market:
    trade_date: dt.date
    next_session: dt.date | None
    sessions: list[dt.date]
    ingest_status: str | None
    #: The ban list for ``next_session``; ``None`` when it has not been stored.
    ban: frozenset[str] | None
    futures: pl.DataFrame
    signals: pl.DataFrame
    expiries: dict[str, list[dt.date]]
    expiry_lots: dict[tuple[str, dt.date], int]
    industries: dict[str, str]

    @property
    def ingested(self) -> bool:
        return self.ingest_status == STATUS_INGESTED

    def settle(self, symbol: str, expiry: dt.date) -> tuple[Decimal, int | None] | None:
        rows = self.futures.filter(
            (pl.col("trade_date") == self.trade_date)
            & (pl.col("symbol") == symbol)
            & (pl.col("expiry") == expiry)
        )
        if rows.is_empty():
            return None
        row = rows.row(0, named=True)
        settle = row["settle"]
        if settle is None or settle <= 0:
            return None
        lot = row["lot_size"]
        return _money(float(settle)) or Decimal(0), None if lot is None else int(lot)

    def listed_futures(self, symbol: str) -> list[dt.date]:
        return sorted(
            set(
                self.futures.filter(
                    (pl.col("trade_date") == self.trade_date) & (pl.col("symbol") == symbol)
                )
                .get_column("expiry")
                .to_list()
            )
        )


async def _futures_window(session: AsyncSession, trade_date: dt.date) -> pl.DataFrame:
    start = trade_date - dt.timedelta(days=SCAN_LOOKBACK_DAYS)
    columns = [FoContractDaily.__table__.c[c] for c in FUTURES_COLUMNS]
    rows = (
        await session.execute(
            select(*columns).where(
                FoContractDaily.trade_date > start,
                FoContractDaily.trade_date <= trade_date,
                FoContractDaily.instrument.in_(_FUTURES),
            )
        )
    ).all()
    schema: dict[str, pl.DataType] = {
        "trade_date": pl.Date(),
        "symbol": pl.String(),
        "instrument": pl.String(),
        "expiry": pl.Date(),
        "open": pl.Float64(),
        "high": pl.Float64(),
        "low": pl.Float64(),
        "settle": pl.Float64(),
        "underlying": pl.Float64(),
        "open_interest": pl.Int64(),
        "turnover": pl.Float64(),
        "lot_size": pl.Int64(),
    }
    return pl.DataFrame(
        [tuple(float(v) if isinstance(v, Decimal) else v for v in row) for row in rows],
        schema=schema,
        orient="row",
    )


async def _expiries(
    session: AsyncSession,
) -> tuple[dict[str, list[dt.date]], dict[tuple[str, dt.date], int]]:
    rows = await session.execute(
        select(OpExpiry.underlying, OpExpiry.expiry_date, OpExpiry.lot_size)
    )
    expiries: dict[str, list[dt.date]] = {}
    lots: dict[tuple[str, dt.date], int] = {}
    for underlying, expiry, lot in rows:
        expiries.setdefault(str(underlying), []).append(expiry)
        lots[(str(underlying), expiry)] = int(lot)
    return expiries, lots


async def _industries(session: AsyncSession, symbols: Iterable[str], on: dt.date) -> dict[str, str]:
    """``symbol -> sector slug``: the narrowest NSE sector index the cash instrument belongs to on
    the latest membership date ≤ ``on`` (FO4.5: the instrument table has no NSE industry)."""
    wanted = sorted(set(symbols))
    if not wanted:
        return {}
    latest = (
        await session.execute(
            select(func.max(IndexMemberDaily.date))
            .join(IndexDef, IndexDef.id == IndexMemberDaily.index_id)
            .where(IndexMemberDaily.date <= on, IndexDef.slug.in_(SECTOR_INDEX_SLUGS))
        )
    ).scalar_one_or_none()
    if latest is None:
        return {}
    rows = await session.execute(
        select(
            Instrument.symbol,
            IndexDef.slug,
            func.count().over(partition_by=IndexMemberDaily.index_id),
        )
        .join(IndexMemberDaily, IndexMemberDaily.instrument_id == Instrument.id)
        .join(IndexDef, IndexDef.id == IndexMemberDaily.index_id)
        .where(
            Instrument.exchange_id == NSE_EXCHANGE_ID,
            IndexMemberDaily.date == latest,
            IndexDef.slug.in_(SECTOR_INDEX_SLUGS),
        )
    )
    best: dict[str, tuple[int, str]] = {}
    targets = set(wanted)
    for symbol, slug, members in rows:
        if symbol not in targets:
            continue
        current = best.get(str(symbol))
        if current is None or int(members) < current[0]:
            best[str(symbol)] = (int(members), str(slug))
    return {symbol: slug for symbol, (_, slug) in best.items()}


async def load_market(session: AsyncSession, trade_date: dt.date, config: FnoConfig) -> Market:
    sessions = await trading_days(
        session,
        trade_date - dt.timedelta(days=SCAN_LOOKBACK_DAYS),
        trade_date + dt.timedelta(days=CALENDAR_AHEAD_DAYS),
    )
    following = next((d for d in sessions if d > trade_date), None)
    ingest = await session.get(FoIngestDay, trade_date)
    ban: frozenset[str] | None = None
    if ingest is not None and following is not None and ingest.ban_for_session == following:
        ban = frozenset(ingest.ban_symbols or ())
    futures = await _futures_window(session, trade_date)
    signals = f2_signals(futures, trade_date, config.f2, config.series)
    expiries, lots = await _expiries(session)
    open_f2 = (
        await session.execute(
            select(FoPosition.symbol).where(
                FoPosition.sleeve == FoSleeve.F2.value, FoPosition.closed_at.is_(None)
            )
        )
    ).scalars()
    universe = signals.filter(pl.col("in_universe")).get_column("symbol").to_list()
    industries = await _industries(session, [*universe, *open_f2], trade_date)
    return Market(
        trade_date=trade_date,
        next_session=following,
        sessions=sessions,
        ingest_status=None if ingest is None else ingest.status,
        ban=ban,
        futures=futures,
        signals=signals,
        expiries=expiries,
        expiry_lots=lots,
        industries=industries,
    )


# --- per-user facts --------------------------------------------------------------------------


@dataclass(slots=True)
class UserBook:
    user_id: int
    capital: dict[str, Decimal]
    #: Stored pauses (``fo_sleeve_config`` by group), in words: hand-set, or ledger-written for
    #: this mode (FO10.1).
    sleeve_pause: dict[str, str]
    book_pause: str | None
    open_positions: dict[str, list[FoPosition]]
    #: ``04`` §7 over the journal, paper rows only, for the session the rows describe (FO10.1).
    auto_pauses: tuple[FoPause, ...]
    events: list[tuple[dt.date, str]]


async def load_user(
    session: AsyncSession,
    user_id: int,
    market: Market,
    *,
    config: FnoConfig = DEFAULT_FNO_CONFIG,
    ceilings: FnoCeilings = DEFAULT_FNO_CEILINGS,
) -> UserBook:
    after = market.next_session or market.trade_date
    simulated = ENTRIES_SIMULATED
    capital: dict[str, Decimal] = {}
    sleeve_pause: dict[str, str] = {}
    for row in (
        await session.execute(select(FoSleeveConfig).where(FoSleeveConfig.user_id == user_id))
    ).scalars():
        capital[row.sleeve] = row.capital_inr
        if (
            column_pause_applies(
                row.paused_until, row.paused_reason, simulated=simulated, day=after
            )
            and row.paused_until is not None
        ):
            sleeve_pause[row.sleeve] = (
                f"sleeve {row.sleeve} is paused until {row.paused_until.isoformat()}"
                f" ({row.paused_reason or 'no reason recorded'})"
            )
    book_pause: str | None = None
    book = await session.get(FoBookConfig, user_id)
    if (
        book is not None
        and book.paused_until is not None
        and column_pause_applies(
            book.paused_until, book.paused_reason, simulated=simulated, day=after
        )
    ):
        book_pause = (
            f"the FO book is paused until {book.paused_until.isoformat()}"
            f" ({book.paused_reason or 'no reason recorded'})"
        )
    auto_pauses = evaluate_pauses(
        await load_trades(session, user_id),
        simulated=simulated,
        as_of=after,
        monthly_pause_inr=Decimal(0) if book is None else Decimal(book.monthly_pause_inr),
        lifted_after=await load_lifts(session, user_id),
        config=config,
        ceilings=ceilings,
    )
    positions: dict[str, list[FoPosition]] = {}
    for position in (
        await session.execute(
            select(FoPosition).where(FoPosition.user_id == user_id, FoPosition.closed_at.is_(None))
        )
    ).scalars():
        positions.setdefault(position.sleeve, []).append(position)
    events = [
        (day, str(reason))
        for day, reason in await session.execute(
            select(OpEventDay.date, OpEventDay.reason)
            .where(OpEventDay.user_id == user_id, OpEventDay.date >= market.trade_date)
            .order_by(OpEventDay.date)
        )
    ]
    return UserBook(user_id, capital, sleeve_pause, book_pause, positions, auto_pauses, events)


# --- F1 -------------------------------------------------------------------------------------------


async def _f1_chain(
    session: AsyncSession, underlying: str, expiry: dt.date, trade_date: dt.date
) -> tuple[list[OptionPrint], dict[tuple[Decimal, OptionType], LegPrint]]:
    rows = await session.execute(
        select(
            FoContractDaily.strike,
            FoContractDaily.option_type,
            FoContractDaily.close,
            FoContractDaily.settle,
            FoContractDaily.open_interest,
            FoContractDaily.volume,
        ).where(
            FoContractDaily.trade_date == trade_date,
            FoContractDaily.symbol == underlying,
            FoContractDaily.expiry == expiry,
            FoContractDaily.instrument.in_(_OPTIONS),
        )
    )
    prints: list[OptionPrint] = []
    legs: dict[tuple[Decimal, OptionType], LegPrint] = {}
    for strike, kind, close, settle, oi, volume in rows:
        option_type = OptionType(kind)
        prints.append(
            OptionPrint(
                expiry=expiry,
                strike=float(strike),
                option_type=option_type,
                close=None if close is None else float(close),
                volume=int(volume or 0),
            )
        )
        legs[(Decimal(strike), option_type)] = LegPrint(Decimal(settle), oi, volume)
    return prints, legs


async def _listed_strikes(
    session: AsyncSession, underlying: str, expiry: dt.date
) -> dict[OptionType, list[Decimal]]:
    rows = await session.execute(
        select(OpContract.option_type, OpContract.strike).where(
            OpContract.underlying == underlying, OpContract.expiry == expiry
        )
    )
    out: dict[OptionType, list[Decimal]] = {OptionType.CE: [], OptionType.PE: []}
    for kind, strike in rows:
        out[OptionType(kind)].append(Decimal(strike))
    return out


def _f1_pauses(book: UserBook, sleeve: FoSleeve) -> list[str]:
    found: list[str] = []
    if book.book_pause is not None:
        found.append(book.book_pause)
    if "F1" in book.sleeve_pause:
        found.append(book.sleeve_pause["F1"])
    found.extend(p.message for p in pauses_for(book.auto_pauses, sleeve))
    return found


def _proposal_detail(p: CondorProposal, expiry: dt.date, lot_size: int) -> JsonObject:
    legs: list[JsonObject] = []
    if p.strikes is not None:
        for seq, role in enumerate(ENTRY_SEQUENCE, start=1):
            legs.append(
                {
                    "entry_seq": seq,
                    "role": role.value,
                    "strike": str(p.strikes.strike(role)),
                    "option_type": option_type_of(role).value,
                    "expiry": expiry.isoformat(),
                    "qty_sign": sign_of(role),
                    "settle": _j(p.entry.get(role)),
                }
            )
    out: JsonObject = {
        "legs": legs,
        "lot_size": lot_size,
        "sd_points": str(_q(p.sd, "0.01")),
        "credit_points": _j(p.credit),
        "max_loss_per_unit": _j(p.max_loss_per_unit),
        "max_loss_per_lot_inr": _j(p.max_loss_per_lot_inr),
    }
    if p.sizing is not None:
        out["sizing"] = {
            "lots": p.sizing.lots,
            "sizing_mode": p.sizing.sizing_mode.value,
            "risk_budget_inr": str(_money(p.sizing.risk_budget_inr)),
            "max_loss_inr": str(_money(p.sizing.max_loss_inr)),
            "message": p.sizing.message,
        }
    if p.cost is not None:
        out["cost"] = {
            "round_trip_inr": str(p.cost.round_trip_inr),
            "credit_inr": str(p.cost.credit_inr),
            "cost_share_pct": _j(p.cost.cost_share_pct),
        }
    return out


async def _f1_row(  # noqa: PLR0911, PLR0913 - one return per state of 04 §8, in order
    session: AsyncSession,
    *,
    market: Market,
    book: UserBook,
    underlying: str,
    config: FnoConfig,
    ceilings: FnoCeilings,
) -> ScanRow:
    sleeve = f1_sleeve_for(underlying)
    s1 = market.next_session
    base: JsonObject = {"underlying": underlying, "next_session": _j(s1)}
    held = book.open_positions.get(sleeve.value, [])
    if held:
        position = held[0]
        return ScanRow(
            underlying,
            ScanState.OPEN_POSITION.value,
            [
                f"an F1 structure on {underlying} is open (plan {position.entry_plan_id}); the "
                "next entry waits for its exit (04 §1)"
            ],
            {
                **base,
                "position_id": position.id,
                "hard_exit_date": _j(position.hard_exit_date),
            },
        )
    if s1 is None:
        return ScanRow(
            underlying,
            ScanState.NO_DATA.value,
            [f"the trading_day calendar has no session after {market.trade_date.isoformat()}"],
            base,
        )
    listed = market.expiries.get(underlying, [])
    if not listed:
        return ScanRow(
            underlying,
            ScanState.NO_DATA.value,
            [f"the NFO master (op_expiry) lists no expiry for {underlying}"],
            base,
        )
    n = config.f1.entry_sessions_before
    found = next_entry(s1, listed, market.sessions, n)
    if found is None:
        return ScanRow(
            underlying,
            ScanState.NO_DATA.value,
            [
                "the trading_day calendar or the NFO master does not reach the entry session of "
                f"a monthly after {s1.isoformat()}"
            ],
            {**base, "monthlies": [d.isoformat() for d in monthly_expiries(listed)]},
        )
    entry, expiry = found
    base = {**base, "next_entry_date": entry.isoformat(), "expiry": expiry.isoformat()}
    pauses = _f1_pauses(book, sleeve)
    if pauses:
        return ScanRow(underlying, ScanState.PAUSED.value, pauses, base)
    if entry != s1:
        return ScanRow(
            underlying,
            ScanState.NOT_ENTRY_DAY.value,
            [
                f"the next entry session is {entry.isoformat()}, {n} sessions before the "
                f"{expiry.isoformat()} monthly; {s1.isoformat()} is not it"
            ],
            base,
        )
    return await _f1_entry_row(
        session,
        market=market,
        book=book,
        underlying=underlying,
        expiry=expiry,
        base=base,
        config=config,
        ceilings=ceilings,
    )


async def _f1_entry_row(  # noqa: PLR0913 - the entry session's inputs, by keyword
    session: AsyncSession,
    *,
    market: Market,
    book: UserBook,
    underlying: str,
    expiry: dt.date,
    base: JsonObject,
    config: FnoConfig,
    ceilings: FnoCeilings,
) -> ScanRow:
    s1 = market.next_session
    assert s1 is not None
    if not market.ingested:
        return ScanRow(
            underlying,
            ScanState.NO_DATA.value,
            [
                f"the bhavcopy for {market.trade_date.isoformat()} is not ingested "
                f"({market.ingest_status or 'never tried'}); nothing is proposed from a guess"
            ],
            base,
        )
    future = market.settle(underlying, expiry)
    if future is None:
        return ScanRow(
            underlying,
            ScanState.NO_DATA.value,
            [f"the bhavcopy has no {underlying} {expiry.isoformat()} future settle"],
            base,
        )
    forward, bhav_lot = future
    prints, legs = await _f1_chain(session, underlying, expiry, market.trade_date)
    atm = atm_iv(
        day=market.trade_date,
        options=prints,
        futures_settle={expiry: float(forward)},
        sessions=market.sessions,
        config=config.series,
    )
    iv = None if atm is None or atm.expiry != expiry else atm.iv
    if iv is None:
        return ScanRow(
            underlying,
            ScanState.NO_DATA.value,
            [f"no ATM IV on the {expiry.isoformat()} monthly: a leg did not trade (04 §4)"],
            {**base, "forward": str(forward)},
        )
    lot_size = market.expiry_lots.get((underlying, expiry)) or bhav_lot
    if lot_size is None or lot_size <= 0:
        return ScanRow(
            underlying,
            ScanState.NO_DATA.value,
            [f"neither the master nor the bhavcopy gives a lot size for {underlying}"],
            base,
        )
    listed = await _listed_strikes(session, underlying, expiry)
    strike_source = "op_contract"
    if not listed[OptionType.CE] or not listed[OptionType.PE]:
        strike_source = "bhavcopy"
        listed = {
            kind: sorted({k for k, t in legs if t is kind})
            for kind in (OptionType.CE, OptionType.PE)
        }
    days = (expiry - s1).days
    proposal = propose_condor(
        forward=forward,
        iv=iv,
        days_to_expiry=days,
        call_strikes=listed[OptionType.CE],
        put_strikes=listed[OptionType.PE],
        prints=legs,
        lot_size=lot_size,
        capital_inr=book.capital.get("F1", Decimal(0)),
        config=config,
        rates=CostRates(),
        ceilings=ceilings,
    )
    hard_exit = hard_exit_session(market.sessions, expiry, config.common.hard_exit_before_expiry)
    last_day = hard_exit or expiry
    events = [(d, r) for d, r in book.events if s1 <= d <= last_day]
    rv = _rv20(market, underlying)
    iv_rv = None if rv is None or rv <= 0 else iv / rv
    detail: JsonObject = {
        **base,
        "entry_session": s1.isoformat(),
        "hard_exit_date": _j(hard_exit),
        "forward": str(forward),
        "iv": iv,
        "rv20": rv,
        "iv_rv": iv_rv,
        "iv_rv_note": "recorded, not used",
        "days_to_expiry": days,
        "strike_source": strike_source,
        "events": [{"date": d.isoformat(), "reason": r} for d, r in events],
        **_proposal_detail(proposal, expiry, lot_size),
    }
    if proposal.sizing is not None:
        detail["lots"] = proposal.sizing.lots
    reasons = list(proposal.reasons)
    if events:
        state = ScanState.SKIPPED_EVENT.value
        named = ", ".join(f"{d.isoformat()} {r}" for d, r in events)
        reasons.insert(
            0,
            f"event day inside the hold {s1.isoformat()} to {last_day.isoformat()}: {named}; "
            "skipped unless Maulik accepts it on the plan (01 §1)",
        )
    elif proposal.state is not None:
        state = proposal.state.value
    else:
        state = ScanState.CANDIDATE.value
    reasons.extend([_IV_RV_NOTE, _SPREAD_NOTE])
    return ScanRow(
        underlying,
        state,
        reasons,
        detail,
        credit=_money(proposal.credit),
        max_loss_per_lot=proposal.max_loss_per_lot_inr,
        cost_share=None if proposal.cost is None else _q(proposal.cost.cost_share_pct, "0.0001"),
        iv=_q(iv, "0.000001"),
        rv20=_q(rv, "0.000001"),
        iv_rv=_q(iv_rv, "0.0001"),
    )


def _rv20(market: Market, symbol: str) -> float | None:
    rows = market.signals.filter(pl.col("symbol") == symbol)
    if rows.is_empty():
        return None
    value = rows.row(0, named=True)["rv20"]
    return None if value is None else float(value)


# --- F2 -------------------------------------------------------------------------------------------


def _f2_pauses(book: UserBook) -> list[str]:
    found: list[str] = []
    if book.book_pause is not None:
        found.append(book.book_pause)
    if "F2" in book.sleeve_pause:
        found.append(book.sleeve_pause["F2"])
    found.extend(p.message for p in pauses_for(book.auto_pauses, FoSleeve.F2))
    return found


def _no_signal_reason(row: Mapping[str, object], config: FnoConfig) -> str:
    parts: list[str] = []
    close = _money(_f(row["close_inr"]))
    if row["breakout"] is not True:
        parts.append(
            f"close {close} is not above the prior {config.f2.breakout_sessions}-session high "
            f"{_money(_f(row['prior_high_inr']))}"
        )
    if row["trend"] is not True:
        parts.append(
            f"close {close} is not above its {config.f2.trend_sessions}-session average "
            f"{_money(_f(row['average_inr']))}"
        )
    return "; ".join(parts)


def _f(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _f2_contract(market: Market, symbol: str, config: FnoConfig) -> tuple[dt.date, str] | None:
    s1 = market.next_session
    if s1 is None:
        return None
    for source, listed in (
        ("op_expiry", market.expiries.get(symbol, [])),
        ("bhavcopy", market.listed_futures(symbol)),
    ):
        if not listed:
            continue
        expiry = f2_contract_expiry(s1, listed, market.sessions, config.f2.near_month_min_sessions)
        if expiry is not None:
            return expiry, source
    return None


def _f2_rows(  # noqa: PLR0912, PLR0915 - the ordered states of 04 §8 for one sleeve
    market: Market, book: UserBook, config: FnoConfig, ceilings: FnoCeilings
) -> list[ScanRow]:
    s1 = market.next_session
    if not market.ingested or s1 is None:
        why = (
            f"the bhavcopy for {market.trade_date.isoformat()} is not ingested "
            f"({market.ingest_status or 'never tried'})"
            if not market.ingested
            else f"the trading_day calendar has no session after {market.trade_date.isoformat()}"
        )
        return [ScanRow(SLEEVE_ROW, ScanState.NO_DATA.value, [why])]
    if market.signals.is_empty():
        return [
            ScanRow(
                SLEEVE_ROW,
                ScanState.NO_DATA.value,
                ["no continuous futures series could be derived from the window"],
            )
        ]
    rows: list[ScanRow] = []
    open_f2 = book.open_positions.get(FoSleeve.F2.value, [])
    held = {p.symbol for p in open_f2}
    for position in open_f2:
        rows.append(
            ScanRow(
                position.symbol,
                ScanState.OPEN_POSITION.value,
                [f"an F2 position in {position.symbol} is open; one per stock (04 §10)"],
                {
                    "position_id": position.id,
                    "stop_price": _j(position.stop_price),
                    "next_roll_date": _j(position.next_roll_date),
                },
            )
        )
    nifty = market.signals.filter(pl.col("symbol") == REGIME_UNDERLYING)
    regime: bool | None = None
    regime_detail: JsonObject = {}
    if not nifty.is_empty():
        n_row = nifty.row(0, named=True)
        trend = n_row["trend"]
        regime = trend if isinstance(trend, bool) else None
        regime_detail = {
            "nifty_close": str(_money(_f(n_row["close_inr"]))),
            "nifty_average": str(_money(_f(n_row["average_inr"]))),
            "nifty_up": regime,
        }
    pauses = _f2_pauses(book)
    capital = book.capital.get("F2", Decimal(0))
    ban_note = (
        None
        if market.ban is not None
        else f"the ban list for {s1.isoformat()} is not stored yet; the 09:20 plan re-checks it"
    )
    queued: list[tuple[float, str, ScanRow]] = []
    universe = market.signals.filter(pl.col("in_universe")).sort("symbol")
    for row in universe.iter_rows(named=True):
        symbol = str(row["symbol"])
        if symbol in held:
            continue
        detail: JsonObject = {
            "turnover_20d": _j(_money(_f(row["turnover_20d"]))),
            "turnover_rank": row["turnover_rank"],
            "universe_size": row["ranked"],
            "close": _j(_money(_f(row["close_inr"]))),
            "breakout_level": _j(_money(_f(row["prior_high_inr"]))),
            "average": _j(_money(_f(row["average_inr"]))),
            "industry": market.industries.get(symbol),
            **regime_detail,
        }
        rv = _f(row["rv20"])
        if market.ban is not None and symbol in market.ban:
            rows.append(
                ScanRow(
                    symbol,
                    ScanState.BLOCKED_BAN.value,
                    [f"{symbol} is in the F&O ban list for {s1.isoformat()} (02 Track C §9)"],
                    detail,
                )
            )
            continue
        if row["ca_recent"]:
            rows.append(
                ScanRow(
                    symbol,
                    ScanState.NO_SIGNAL.value,
                    [
                        "a corporate action (a > 30 % move in the held future) in the last "
                        f"{config.series.ca_exclusion_sessions} sessions excludes it from "
                        "signals (04 §4)"
                    ],
                    detail,
                )
            )
            continue
        if row["breakout"] is None or row["trend"] is None:
            rows.append(
                ScanRow(
                    symbol,
                    ScanState.NO_DATA.value,
                    [
                        f"{row['good_sessions']} good sessions in the window; the breakout and "
                        f"{config.f2.trend_sessions}-session average need more"
                    ],
                    detail,
                )
            )
            continue
        if not (row["breakout"] and row["trend"]):
            rows.append(
                ScanRow(symbol, ScanState.NO_SIGNAL.value, [_no_signal_reason(row, config)], detail)
            )
            continue
        if regime is None:
            rows.append(
                ScanRow(
                    symbol,
                    ScanState.NO_DATA.value,
                    [
                        f"NIFTY's continuous future has no {config.f2.trend_sessions}-session "
                        "average in the window, so the regime cannot be read"
                    ],
                    detail,
                )
            )
            continue
        if not regime:
            rows.append(
                ScanRow(
                    symbol,
                    ScanState.BLOCKED_REGIME.value,
                    [
                        f"NIFTY's future {regime_detail.get('nifty_close')} is below its "
                        f"{config.f2.trend_sessions}-session average "
                        f"{regime_detail.get('nifty_average')} (04 §10)"
                    ],
                    detail,
                )
            )
            continue
        if pauses:
            rows.append(ScanRow(symbol, ScanState.PAUSED.value, list(pauses), detail))
            continue
        contract = _f2_contract(market, symbol, config)
        future = None if contract is None else market.settle(symbol, contract[0])
        atr = _money(_f(row["atr_inr"]))
        if contract is None or future is None or atr is None or atr <= 0:
            missing = (
                "no monthly contract more than "
                f"{config.f2.near_month_min_sessions} sessions from expiry"
                if contract is None
                else f"no {contract[0].isoformat()} settle in the bhavcopy"
                if future is None
                else "no ATR14 in the window"
            )
            rows.append(ScanRow(symbol, ScanState.NO_DATA.value, [missing], detail))
            continue
        expiry, source = contract
        entry, bhav_lot = future
        lot_size = market.expiry_lots.get((symbol, expiry)) or bhav_lot
        if lot_size is None or lot_size <= 0:
            rows.append(
                ScanRow(
                    symbol,
                    ScanState.NO_DATA.value,
                    [f"neither the master nor the bhavcopy gives a lot size for {symbol}"],
                    detail,
                )
            )
            continue
        proposal = propose_future(
            entry=entry,
            atr=atr,
            ann_vol=rv or 0.0,
            lot_size=lot_size,
            capital_inr=capital,
            config=config,
            ceilings=ceilings,
        )
        detail = {**detail, **_future_detail(proposal, expiry, source, lot_size)}
        scan_row = ScanRow(
            symbol,
            ScanState.CANDIDATE.value,
            _candidate_reasons(proposal, config, ceilings),
            detail,
            max_loss_per_lot=proposal.risk_per_lot_inr,
            cost_share=_q(proposal.cost_share_pct, "0.0001"),
            rv20=_q(rv, "0.000001"),
        )
        if ban_note is not None:
            scan_row.reasons.append(ban_note)
        if proposal.sizing.state is not None:
            scan_row.state = proposal.sizing.state.value
            scan_row.reasons.insert(0, proposal.sizing.message)
            rows.append(scan_row)
            continue
        queued.append((_f(row["turnover_20d"]) or 0.0, symbol, scan_row))
    queued.sort(key=lambda item: (-item[0], item[1]))
    verdicts = allocate_capacity(
        [(symbol, market.industries.get(symbol)) for _, symbol, _ in queued],
        [market.industries.get(p.symbol) for p in open_f2],
        config.f2,
    )
    for _, symbol, scan_row in queued:
        blocked = verdicts.get(symbol)
        if blocked is not None:
            scan_row.state = ScanState.BLOCKED_CAPACITY.value
            scan_row.reasons.insert(0, f"{blocked} (04 §10)")
        rows.append(scan_row)
    return rows


def _future_detail(p: FutureProposal, expiry: dt.date, source: str, lot_size: int) -> JsonObject:
    return {
        "contract_expiry": expiry.isoformat(),
        "expiry_source": source,
        "entry_reference": str(p.entry),
        "atr14": str(p.atr),
        "stop": str(p.stop),
        "gtt_trigger": str(p.gtt_trigger),
        "risk_per_unit": str(_money(p.risk_per_unit)),
        "risk_per_lot_inr": str(p.risk_per_lot_inr),
        "lot_size": lot_size,
        "lots": p.sizing.lots,
        "sizing_mode": p.sizing.sizing_mode.value,
        "lots_at_ceiling": p.sizing.lots_at_ceiling,
        "round_trip_inr": str(p.cost.total),
        "cost_share_pct": str(p.cost_share_pct),
    }


def _candidate_reasons(p: FutureProposal, config: FnoConfig, ceilings: FnoCeilings) -> list[str]:
    reasons = [
        f"close above the prior {config.f2.breakout_sessions}-session high and its "
        f"{config.f2.trend_sessions}-session average, NIFTY above its own (04 §10)",
    ]
    if p.sizing.lots_at_ceiling is not None:
        reasons.append(p.sizing.message)
        if p.sizing.lots_at_ceiling == 0:
            reasons.append(
                f"live would be REJECTED_SIZE: one lot risks ₹{p.risk_per_lot_inr}, above the "
                f"₹{ceilings.risk_per_trade_inr_max} per-trade ceiling"
            )
    return reasons


# --- write ----------------------------------------------------------------------------------------


async def write_rows(
    session: AsyncSession,
    user_id: int,
    sleeve: FoSleeve,
    trade_date: dt.date,
    rows: Sequence[ScanRow],
) -> int:
    """Upsert on ``(user_id, sleeve, trade_date, symbol)`` and delete what the run no longer
    produces, so a re-run leaves exactly this set (house rule 7)."""
    symbols = [r.symbol for r in rows]
    if len(set(symbols)) != len(symbols):
        raise ValueError(f"{sleeve.value}: a symbol appears twice in one scan")
    stale = delete(FoScan).where(
        FoScan.user_id == user_id, FoScan.sleeve == sleeve.value, FoScan.trade_date == trade_date
    )
    if symbols:
        stale = stale.where(FoScan.symbol.not_in(symbols))
    await session.execute(stale)
    if not rows:
        return 0
    values = [
        {
            "user_id": user_id,
            "sleeve": sleeve.value,
            "trade_date": trade_date,
            "symbol": r.symbol,
            "state": r.state,
            "reasons": r.reasons,
            "detail": _j(r.detail),
            "credit": r.credit,
            "max_loss_per_lot": r.max_loss_per_lot,
            "cost_share": r.cost_share,
            "iv": r.iv,
            "rv20": r.rv20,
            "iv_rv": r.iv_rv,
        }
        for r in rows
    ]
    stmt = insert(FoScan).values(values)
    updated = (
        "state",
        "reasons",
        "detail",
        "credit",
        "max_loss_per_lot",
        "cost_share",
        "iv",
        "rv20",
        "iv_rv",
    )
    await session.execute(
        stmt.on_conflict_do_update(
            constraint="uq_fo_scan_user_sleeve_date_symbol",
            set_={c: stmt.excluded[c] for c in updated},
        )
    )
    await session.flush()
    return len(rows)


# --- the run --------------------------------------------------------------------------------------


async def scan_users(session: AsyncSession) -> list[int]:
    """Every user with an ``fo_sleeve_config`` row — the FO run's tenants (FO4.2)."""
    rows = await session.execute(select(FoSleeveConfig.user_id).distinct())
    return sorted(int(u) for u in rows.scalars())


async def run_scan(
    session: AsyncSession,
    trade_date: dt.date,
    *,
    user_ids: Sequence[int] | None = None,
    config: FnoConfig = DEFAULT_FNO_CONFIG,
    ceilings: FnoCeilings = DEFAULT_FNO_CEILINGS,
) -> JsonObject:
    """``fo_scan`` for the session after ``trade_date``, for each user. No network."""
    users = list(user_ids) if user_ids is not None else await scan_users(session)
    if not users:
        return {
            "trade_date": trade_date.isoformat(),
            "skipped": "no user has fo_sleeve_config rows (fno_cli seed)",
        }
    market = await load_market(session, trade_date, config)
    out: JsonObject = {
        "trade_date": trade_date.isoformat(),
        "next_session": _j(market.next_session),
        "ingest_status": market.ingest_status,
    }
    per_user: dict[str, object] = {}
    for user_id in users:
        book = await load_user(session, user_id, market, config=config, ceilings=ceilings)
        written: dict[str, dict[str, int]] = {}
        for underlying in config.f1.underlyings:
            row = await _f1_row(
                session,
                market=market,
                book=book,
                underlying=underlying,
                config=config,
                ceilings=ceilings,
            )
            sleeve = f1_sleeve_for(underlying)
            await write_rows(session, user_id, sleeve, trade_date, [row])
            written[sleeve.value] = {row.state: 1}
        f2 = _f2_rows(market, book, config, ceilings)
        await write_rows(session, user_id, FoSleeve.F2, trade_date, f2)
        counts: dict[str, int] = {}
        for row in f2:
            counts[row.state] = counts.get(row.state, 0) + 1
        written[FoSleeve.F2.value] = counts
        per_user[str(user_id)] = written
    out["users"] = per_user
    log.info("fo_scan %s: %s", trade_date.isoformat(), per_user)
    return out


__all__ = [
    "SLEEVE_ROW",
    "ScanRow",
    "load_market",
    "run_scan",
    "scan_users",
    "write_rows",
]
