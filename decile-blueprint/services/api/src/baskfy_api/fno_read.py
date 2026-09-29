"""The FO run's reads for the web app (FO5, ``docs/fno/05`` §2-§3, ``04`` §5).

Everything here reads what the nightly ingest, the scan and (later) the desk wrote —
``fo_scan``, ``fo_position``, ``fo_mark``, ``fo_journal``, ``fo_backtest_run``,
``fo_underlying_daily``, ``fo_contract_daily``, ``fo_ingest_day``, ``fo_spread_sample``,
``op_expiry`` — and **computes no signal**. The only arithmetic is descriptive: the 1-year IV
percentile of a stored ``iv_atm``, a 5-session change of a stored ``oi_total``, calendar days to
a listed expiry, and counts. None of it is named or ordered as a signal (``04`` §5, PACK.8).

THE CLOCK (root ``CLAUDE.md``'s two-clock table, the Overnight and Stock F&O rows)
---------------------------------------------------------------------------------
Every number here is end of day: the scan's ``trade_date`` (the bhavcopy it read), the marks'
settle, the series' latest session. The page states it (``As of close, Tue 22 Sep``). Nothing here
reaches Kite; the page's only live overlay is the shared ``useLiveMarks`` read on the underlying's
level (overnight) — the web app's, not this module's.

EVIDENCE TEXT IS QUOTED, NOT WRITTEN
------------------------------------
``RESEARCH_*`` and ``TIER_2E_CAVEAT`` are ``docs/fno/RESEARCH.md`` §B4 / the verdict table and
``07`` §4's Tier 2E caveat, verbatim (``07`` §4: "No Tier 2E number is shown without its caveat").
The research directory is not shipped with the API, so the numbers of the 23 Sep 2026 run are
quoted here once; a later re-test arrives as ``fo_backtest_run`` rows, shown beside them.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from pydantic import BaseModel
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.fno import directional_retest
from baskfy_core.fno.calendar import monthly_expiries
from baskfy_core.fno.config import FoSleeve, FoSleeveGroup, group_of
from baskfy_core.models import (
    FoBacktestRun,
    FoContractDaily,
    FoIngestDay,
    FoJournal,
    FoMark,
    FoPosition,
    FoScan,
    FoSpreadSample,
    FoUnderlyingDaily,
    OpExpiry,
    TradingDay,
)
from baskfy_core.models.base import JsonObject
from baskfy_core.seed_data import NSE_EXCHANGE_ID

# --- quoted evidence ------------------------------------------------------------------------------

#: ``07`` §4, the Tier 2E row's "Must say on the card", verbatim.
TIER_2E_CAVEAT: Final = (
    "End-of-day closes, not fills. Slippage is an assumed 3 % of premium per leg per crossing "
    "(0.03 % for futures), not measured. `n` legs were modelled because they did not trade."
)
#: ``RESEARCH.md`` verdict table, row B4, as tested (23 Sep 2026 run).
RESEARCH_B4_LINE: Final = (
    "B4 Index monthly iron condor, entered 15 sessions before expiry (NIFTY + BANKNIFTY): "
    "+0.033R a trade (t = 1.36, n = 100, 86 % win, max DD -1.64R)"
)
#: ``RESEARCH.md`` §B4, "Maulik's loss close" — F1 as built (M.1).
RESEARCH_B4_LOSS_CLOSE_LINE: Final = (
    "With Maulik's loss close at 1.5 x the credit (F1 as built): +0.022R (t = 1.01), win 83 %, "
    "worst -0.73R, max drawdown -1.35R. By year: -0.010, -0.004, +0.078, +0.010, +0.047"
)
#: ``RESEARCH.md`` §B4: the index condor's own slippage assumption.
RESEARCH_B4_SLIPPAGE: Final = "B4 itself assumed 0.5 % of premium per leg per crossing"
RESEARCH_B4_N: Final = 100
RESEARCH_RUN_DATE: Final = dt.date(2026, 9, 23)
RESEARCH_SAMPLE: Final = "3 Jan 2022 to 22 Sep 2026"
#: ``RESEARCH.md`` §B4, "Why it is only a paper candidate", in one line each.
RESEARCH_B4_WHY_PAPER: Final[tuple[str, ...]] = (
    "N=15 was chosen after seeing N=10 and N=20; the neighbouring choices are negative",
    "t = 1.36 on 100 trades is not significant",
    "the IV ÷ RV > 1.3 slice is 24 trades found in-sample, a hypothesis, not a filter",
    "the years are thinning: ~0 in 2025 and 2026",
)
#: ``RESEARCH.md`` §A, F2 re-costed with its rolls (M.1).
RESEARCH_F2_LINE: Final = (
    "F2, the long-only chandelier, re-costed with its rolls: n = 2,334, +0.017R (t = 0.71), "
    "win 39 %. By year: -0.026, +0.455, -0.129, -0.143, -0.141"
)

#: F3-3's EOD re-test of F3's daily rules (``docs/fno/evidence/f3-retest.md``, 28 Sep 2026).
RESEARCH_F3_LINE: Final = (
    "The daily half of F3's rules, re-tested on the closing files 3 Jan 2022 to 22 Sep 2026, "
    "after costs: NIFTY n = 206, -0.021R a trade (t = -2.26, 67 % win); BANKNIFTY n = 166, "
    "-0.020R (t = -1.60, 57 % win); near zero before costs. Whatever edge the method has must "
    "come from the intraday rules, which only the paper period can test"
)
#: F3's underlyings in ``04`` §8's order, with the sleeve each trades (M.5).
F3_UNDERLYINGS: Final[tuple[tuple[str, str], ...]] = (
    ("NIFTY", FoSleeve.F3N.value),
    ("BANKNIFTY", FoSleeve.F3B.value),
)
#: The four rules a closing file cannot test, as the proxy names them.
F3_NOT_TESTED: Final[tuple[str, ...]] = directional_retest.NOT_TESTED
#: The quarterly re-test's F3 families (``baskfy_core.fno.retest``, F3-7).
F3_FAMILIES: Final = frozenset({"F3N", "F3B"})


class VerdictRow(BaseModel):
    """One row of ``RESEARCH.md``'s verdict table."""

    family: str
    best_variant: str
    net_r: str
    robust: str
    verdict: str
    #: ``fo_backtest_run.family`` codes whose latest run is this row's re-test.
    retest_families: list[str]


def _v(  # noqa: PLR0913, PLR0917 - one argument per column of the table
    family: str, variant: str, net: str, robust: str, verdict: str, codes: Sequence[str]
) -> VerdictRow:
    return VerdictRow(
        family=family,
        best_variant=variant,
        net_r=net,
        robust=robust,
        verdict=verdict,
        retest_families=list(codes),
    )


#: ``RESEARCH.md`` "The verdict first", in its order, markdown stripped.
VERDICT_TABLE: Final[tuple[VerdictRow, ...]] = (
    _v(
        "B4 Index monthly iron condor, entered 15 sessions before expiry (NIFTY + BANKNIFTY)",
        "short ±1 sd, wings +0.5 sd, 50 % profit take, exit before expiry",
        "+0.033 (t = 1.36, n = 100, 86 % win, max DD -1.64R)",
        "≥ 0 in 2022, 2023, 2024; ~0 in 2025 and 2026",
        "The one paper candidate (F1), with the caveats in §B4",
        ("B4", "B4_LOSS_CLOSE"),
    ),
    _v(
        "A Stock-futures trend (20-day breakout with trend + NIFTY regime)",
        "3 ATR chandelier, long only",
        "+0.033 (t = 1.35, n = 2,334)",
        "No. +0.47 in 2023, negative in 2022, 2024, 2025 and 2026",
        "Not built. One year carries it (built on paper as F2 by Maulik's choice, M.1)",
        ("A0_LONG", "F2"),
    ),
    _v(
        "A Stock-futures trend, both sides",
        "as above",
        "-0.023",
        "shorts lose in 4 of 5 years",
        "Not built",
        ("A0",),
    ),
    _v(
        'A Open-interest "buildup" filters',
        "OI rising with the breakout; 1-day long/short buildup",
        "-0.05 to -0.01",
        "—",
        "OI adds nothing; the control without OI does better",
        ("A1", "A1R", "A3"),
    ),
    _v(
        "A Cross-sectional momentum, futures long/short",
        "top/bottom 10 weekly",
        "-0.004",
        "—",
        "Not built",
        ("A2",),
    ),
    _v(
        "A Index-futures trend",
        "NIFTY, BANKNIFTY, MIDCPNIFTY",
        "-0.03 to -0.06",
        "—",
        "Not built",
        ("A4",),
    ),
    _v(
        "B1 Stock iron condors (top 30 by liquidity)",
        "N = 10, ±1 sd",
        "-0.021 at 1.5 % slip, -0.032 at 3 %",
        "negative every year",
        "Rejected: no gross edge (+0.005 to +0.027R before costs)",
        ("B1",),
    ),
    _v(
        "B2 Stock credit spreads on the trend's side",
        "N = 15, 0.5 sd",
        "-0.024 to -0.042",
        "—",
        "Rejected (gross +0.006 to +0.008)",
        ("B2",),
    ),
    _v(
        "B3 Exit at E-4 instead of E-1 (stock condors)",
        "—",
        "-0.042 to -0.055",
        "—",
        "Worse. The delivery-margin ramp is avoided at a cost in edge. Moot while B1 is rejected",
        ("B3",),
    ),
    _v(
        "C1 Debit spreads on the breakout signal",
        "ATM / +0.5 sd, 10 sessions",
        "-0.236 (t = -20.8)",
        "—",
        "Rejected",
        ("C1",),
    ),
    _v(
        "C2 Long ATM option on the breakout signal",
        "10 sessions",
        "-0.105",
        "—",
        "Rejected",
        ("C2",),
    ),
    _v(
        "E Cash-futures carry",
        "front month, ≥ 5 days to expiry",
        "median basis 5.6 % a.y. before ~0.45 %/cycle costs; 0.7 % of stock-days clear 7 % net",
        "—",
        "Rejected: below the cost of the capital",
        ("E",),
    ),
)

#: ``04`` §9's paper periods, stated once for the tally.
F1_PAPER_CYCLES_PER_UNDERLYING: Final = 6
F1_PAPER_MIN_OPENED: Final = 4
F2_PAPER_SESSIONS: Final = 60
F2_PAPER_MIN_CLOSED: Final = 15
F2_PAPER_MIN_ROLLS: Final = 3

#: FO5.4: a 1-year IV percentile needs this many stored ``iv_atm`` sessions in the year, else null.
IV_PERCENTILE_MIN_SESSIONS: Final = 200
IV_PERCENTILE_WINDOW_DAYS: Final = 365
OI_CHANGE_SESSIONS: Final = 5
#: How far back the status line looks for ``MISSING`` nights.
MISSING_LOOKBACK_DAYS: Final = 90
JOURNAL_LIMIT: Final = 100

#: F1's underlyings and the symbol the shared live overlay knows each index by.
F1_UNDERLYINGS: Final[tuple[str, ...]] = ("NIFTY", "BANKNIFTY")
LIVE_SYMBOL: Final[Mapping[str, str]] = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK"}
#: The F2 states the candidates panel lists (``05`` §2: candidates and ``REJECTED_SIZE``).
F2_LISTED_STATES: Final[tuple[str, ...]] = ("CANDIDATE", "REJECTED_SIZE")
F2_WHOLE_SLEEVE_SYMBOL: Final = "*"
FUTURE: Final = "XX"

EMPTY_SCAN_OFF: Final = "scan_off"
EMPTY_NEVER: Final = "never_scanned"


# --- response models ------------------------------------------------------------------------------


class FnoScanOut(BaseModel):
    """One ``fo_scan`` row. ``detail`` is the worker's JSONB passed through: its money figures are
    already decimal strings (FO4), and re-rendering them here would round a second time."""

    sleeve: str
    trade_date: dt.date
    symbol: str
    state: str
    reasons: list[str]
    detail: JsonObject
    credit: Decimal | None
    max_loss_per_lot: Decimal | None
    cost_share: Decimal | None
    iv: Decimal | None
    rv20: Decimal | None
    iv_rv: Decimal | None


class FnoMarkOut(BaseModel):
    trade_date: dt.date
    mark_points: Decimal
    pnl_inr: Decimal
    #: ``pnl_inr`` over the position's max loss (F1) or its risk at entry (F2); null when unknown.
    pnl_r: Decimal | None
    stop_price: Decimal | None
    detail: JsonObject


class FnoPositionOut(BaseModel):
    id: int
    sleeve: str
    symbol: str
    structure: str
    entry_plan_id: str
    legs: JsonObject
    lots: int
    lot_size: int
    entry_price: Decimal | None
    entry_credit: Decimal | None
    max_loss_inr: Decimal | None
    profit_take_points: Decimal | None
    loss_close_points: Decimal | None
    stop_price: Decimal | None
    gtt_id: str | None
    hard_exit_date: dt.date | None
    next_roll_date: dt.date | None
    opened_on: dt.date
    sessions_held: int | None
    simulated: bool
    mark: FnoMarkOut | None


class FnoJournalOut(BaseModel):
    position_id: int
    sleeve: str
    symbol: str
    structure: str
    opened_on: dt.date
    closed_on: dt.date
    net_pnl_inr: Decimal
    costs_inr: Decimal
    r_multiple: Decimal
    closed_reason: str
    sessions_held: int
    rolls: int
    simulated: bool
    sizing_mode: str


class FnoBacktestOut(BaseModel):
    """One ``fo_backtest_run`` row, caveat on the row (``07`` §4)."""

    family: str
    tier: str
    caveat: str
    sample_from: dt.date
    sample_to: dt.date
    n: int
    net_r: Decimal | None
    gross_r: Decimal | None
    per_year: JsonObject
    slippage_source: str
    run_at: dt.datetime


class FnoLevelOut(BaseModel):
    """The underlying's level from the session's bhavcopy (the file's ``underlying`` column),
    and the symbol the shared live overlay knows it by."""

    level: Decimal | None
    close_of: dt.date | None
    live_symbol: str


class FnoUnderlyingOut(BaseModel):
    symbol: str
    sleeve: str
    level: FnoLevelOut
    scan: FnoScanOut | None
    next_entry_date: dt.date | None


class FnoTallyOut(BaseModel):
    """``04`` §9 against the paper record. Violations are FO10's ledger; until then ``None``."""

    sleeve: str
    target: str
    closed: int
    opened_cycles: int
    rolls: int
    first_opened: dt.date | None
    violations: int | None


class FnoEvidenceOut(BaseModel):
    tier: str
    line: str
    loss_close_line: str
    slippage_note: str
    caveat: str
    n: int
    run_date: dt.date
    sample: str
    why_paper: list[str]
    backtests: list[FnoBacktestOut]
    tally: list[FnoTallyOut]


class FnoF2Out(BaseModel):
    research_line: str
    scan_date: dt.date | None
    candidates: list[FnoScanOut]
    #: The ``*`` row when the sleeve could not be evaluated at all (FO4.3).
    sleeve_row: FnoScanOut | None
    #: Every state of the night's F2 rows, counted — so "no candidate" says what the others were.
    state_counts: dict[str, int]
    open: list[FnoPositionOut]
    closed: list[FnoJournalOut]


class FnoF3Out(BaseModel):
    """F3, the directional index credit spread (M.5): what the night's scan read per underlying,
    the open spreads marked at settle, the closed ones, and the evidence it stands on. Read-only
    like the rest of the page: the desk's ``/fno`` holds the plan, the exit and the add."""

    research_line: str
    #: The rules a closing file cannot test (``directional_retest.NOT_TESTED``), named on the card.
    not_tested: list[str]
    scan_date: dt.date | None
    underlyings: list[FnoUnderlyingOut]
    open: list[FnoPositionOut]
    closed: list[FnoJournalOut]
    #: The latest ``F3N``/``F3B`` quarterly re-test rows, when one has run.
    backtests: list[FnoBacktestOut]


class FnoGateOut(BaseModel):
    group: str
    #: ``PAPER`` unless the sleeve's execution flag is on; reported, never branched on here.
    mode: str


class FnoOvernightOut(BaseModel):
    today: dt.date
    next_session: dt.date | None
    scan_date: dt.date | None
    empty_reason: str | None
    scan_enabled: bool
    monitor_enabled: bool
    gates: list[FnoGateOut]
    underlyings: list[FnoUnderlyingOut]
    #: Open structures whose ``hard_exit_date`` is the next session (``05`` §2's red line).
    hard_exit_tomorrow: list[FnoPositionOut]
    open_structures: list[FnoPositionOut]
    journal: list[FnoJournalOut]
    evidence: FnoEvidenceOut
    f2: FnoF2Out
    f3: FnoF3Out


class FnoInfoRowOut(BaseModel):
    """``04`` §5's columns. No field is named or computed as a signal."""

    symbol: str
    lot_size: int | None
    near_monthly: dt.date | None
    days_to_near_monthly: int | None
    fut_settle: Decimal | None
    basis_ann: Decimal | None
    oi_change_5d_pct: Decimal | None
    iv: Decimal | None
    rv20: Decimal | None
    iv_rv: Decimal | None
    iv_pct_1y: Decimal | None
    iv_sessions_1y: int
    in_ban: bool
    fut_turnover_20d: Decimal | None


class FnoFamilyOut(BaseModel):
    verdict: VerdictRow
    latest_retest: FnoBacktestOut | None


class FnoSpreadOut(BaseModel):
    sessions: int
    first: dt.date | None
    last: dt.date | None
    symbols: int


class FnoIngestOut(BaseModel):
    latest_date: dt.date | None
    latest_status: str | None
    missing_days: list[dt.date]
    ban_for_session: dt.date | None
    ban_symbols: list[str]


class FnoInfoOut(BaseModel):
    as_of: dt.date | None
    rows: list[FnoInfoRowOut]
    families: list[FnoFamilyOut]
    research_run_date: dt.date
    research_sample: str
    spread_sample: FnoSpreadOut
    ingest: FnoIngestOut


# --- helpers --------------------------------------------------------------------------------------


def _q(value: Decimal, places: str) -> Decimal:
    """Round at write time of the response (house rule 8), half up."""
    return value.quantize(Decimal(places), rounding=ROUND_HALF_UP)


async def next_session_after(session: AsyncSession, day: dt.date) -> dt.date | None:
    """The first NSE session after ``day`` on ``trading_day``; the next weekday if the calendar
    does not reach that far (a check must not fall silent — ``swing_health.is_session_day``)."""
    found = (
        await session.execute(
            select(func.min(TradingDay.date)).where(
                TradingDay.exchange_id == NSE_EXCHANGE_ID,
                TradingDay.date > day,
                TradingDay.is_trading_day.is_(True),
            )
        )
    ).scalar_one_or_none()
    if found is not None:
        return found
    candidate = day + dt.timedelta(days=1)
    while candidate.weekday() >= 5:  # noqa: PLR2004 - Saturday
        candidate += dt.timedelta(days=1)
    return candidate


def scan_out(row: FoScan) -> FnoScanOut:
    return FnoScanOut(
        sleeve=str(row.sleeve),
        trade_date=row.trade_date,
        symbol=row.symbol,
        state=row.state,
        reasons=list(row.reasons or []),
        detail=dict(row.detail or {}),
        credit=row.credit,
        max_loss_per_lot=row.max_loss_per_lot,
        cost_share=row.cost_share,
        iv=row.iv,
        rv20=row.rv20,
        iv_rv=row.iv_rv,
    )


def journal_out(row: FoJournal) -> FnoJournalOut:
    return FnoJournalOut(
        position_id=row.position_id,
        sleeve=str(row.sleeve),
        symbol=row.symbol,
        structure=row.structure,
        opened_on=row.opened_on,
        closed_on=row.closed_on,
        net_pnl_inr=row.net_pnl_inr,
        costs_inr=row.costs_inr,
        r_multiple=row.r_multiple,
        closed_reason=row.closed_reason,
        sessions_held=row.sessions_held,
        rolls=row.rolls,
        simulated=row.simulated,
        sizing_mode=row.sizing_mode,
    )


def backtest_out(row: FoBacktestRun) -> FnoBacktestOut:
    return FnoBacktestOut(
        family=row.family,
        tier=row.tier,
        caveat=row.caveat,
        sample_from=row.sample_from,
        sample_to=row.sample_to,
        n=row.n,
        net_r=row.net_r,
        gross_r=row.gross_r,
        per_year=dict(row.per_year or {}),
        slippage_source=row.slippage_source,
        run_at=row.run_at,
    )


def _risk_of(position: FoPosition) -> Decimal | None:
    """One R of an open position: its max loss at entry as the desk recorded it (``03`` §5);
    null — and the page shows no R — when the desk has not recorded one."""
    if position.max_loss_inr is not None and position.max_loss_inr > 0:
        return position.max_loss_inr
    return None


async def _sessions_between(session: AsyncSession, start: dt.date, end: dt.date) -> int:
    """Trading sessions in ``[start, end]`` on ``trading_day`` — entry day is session 1 (FO1.3)."""
    count = (
        await session.execute(
            select(func.count()).where(
                TradingDay.exchange_id == NSE_EXCHANGE_ID,
                TradingDay.date >= start,
                TradingDay.date <= end,
                TradingDay.is_trading_day.is_(True),
            )
        )
    ).scalar_one()
    return int(count)


async def open_positions(
    session: AsyncSession, user_id: int, *, today: dt.date, sleeves: Sequence[str] | None = None
) -> list[FnoPositionOut]:
    """Open structures and futures, each with its latest settle mark (``fo_mark``)."""
    stmt = select(FoPosition).where(FoPosition.user_id == user_id, FoPosition.closed_at.is_(None))
    if sleeves is not None:
        stmt = stmt.where(FoPosition.sleeve.in_(list(sleeves)))
    positions = list((await session.execute(stmt.order_by(FoPosition.opened_at))).scalars())
    if not positions:
        return []
    ids = [p.id for p in positions]
    latest = (
        select(FoMark.position_id, func.max(FoMark.trade_date).label("d"))
        .where(FoMark.position_id.in_(ids))
        .group_by(FoMark.position_id)
        .subquery()
    )
    marks = {
        m.position_id: m
        for m in (
            await session.execute(
                select(FoMark).join(
                    latest,
                    and_(
                        FoMark.position_id == latest.c.position_id, FoMark.trade_date == latest.c.d
                    ),
                )
            )
        ).scalars()
    }
    out: list[FnoPositionOut] = []
    for p in positions:
        mark = marks.get(p.id)
        risk = _risk_of(p)
        opened_on = p.opened_at.date()
        out.append(
            FnoPositionOut(
                id=p.id,
                sleeve=str(p.sleeve),
                symbol=p.symbol,
                structure=p.structure,
                entry_plan_id=p.entry_plan_id,
                legs=dict(p.legs or {}),
                lots=p.lots,
                lot_size=p.lot_size,
                entry_price=p.entry_price,
                entry_credit=p.entry_credit,
                max_loss_inr=p.max_loss_inr,
                profit_take_points=p.profit_take_points,
                loss_close_points=p.loss_close_points,
                stop_price=p.stop_price,
                gtt_id=p.gtt_id,
                hard_exit_date=p.hard_exit_date,
                next_roll_date=p.next_roll_date,
                opened_on=opened_on,
                sessions_held=await _sessions_between(session, opened_on, today),
                simulated=p.simulated,
                mark=None
                if mark is None
                else FnoMarkOut(
                    trade_date=mark.trade_date,
                    mark_points=mark.mark_points,
                    pnl_inr=mark.pnl_inr,
                    pnl_r=None if risk is None else _q(mark.pnl_inr / risk, "0.001"),
                    stop_price=mark.stop_price,
                    detail=dict(mark.detail or {}),
                ),
            )
        )
    return out


async def journal(
    session: AsyncSession, user_id: int, sleeves: Sequence[str]
) -> list[FnoJournalOut]:
    rows = await session.execute(
        select(FoJournal)
        .where(FoJournal.user_id == user_id, FoJournal.sleeve.in_(list(sleeves)))
        .order_by(FoJournal.closed_on.desc(), FoJournal.position_id.desc())
        .limit(JOURNAL_LIMIT)
    )
    return [journal_out(r) for r in rows.scalars()]


async def latest_scan_date(
    session: AsyncSession, user_id: int, sleeves: Sequence[str]
) -> dt.date | None:
    return (
        await session.execute(
            select(func.max(FoScan.trade_date)).where(
                FoScan.user_id == user_id, FoScan.sleeve.in_(list(sleeves))
            )
        )
    ).scalar_one_or_none()


async def scan_rows(
    session: AsyncSession, user_id: int, day: dt.date, sleeves: Sequence[str]
) -> list[FoScan]:
    rows = await session.execute(
        select(FoScan)
        .where(
            FoScan.user_id == user_id,
            FoScan.trade_date == day,
            FoScan.sleeve.in_(list(sleeves)),
        )
        .order_by(FoScan.sleeve, FoScan.symbol)
    )
    return list(rows.scalars())


async def latest_backtests(session: AsyncSession, user_id: int) -> list[FnoBacktestOut]:
    """The newest ``fo_backtest_run`` per family (``04`` §6; append-only, FO9 writes it)."""
    newest = (
        select(FoBacktestRun.family, func.max(FoBacktestRun.run_at).label("at"))
        .where(FoBacktestRun.user_id == user_id)
        .group_by(FoBacktestRun.family)
        .subquery()
    )
    rows = await session.execute(
        select(FoBacktestRun)
        .join(
            newest,
            and_(FoBacktestRun.family == newest.c.family, FoBacktestRun.run_at == newest.c.at),
        )
        .where(FoBacktestRun.user_id == user_id)
        .order_by(FoBacktestRun.family, FoBacktestRun.id.desc())
    )
    seen: set[str] = set()
    out: list[FnoBacktestOut] = []
    for row in rows.scalars():
        if row.family in seen:
            continue
        seen.add(row.family)
        out.append(backtest_out(row))
    return out


async def underlying_level(session: AsyncSession, symbol: str, day: dt.date | None) -> FnoLevelOut:
    """The index level the bhavcopy printed beside the session's futures (UDiFF days only)."""
    live_symbol = LIVE_SYMBOL.get(symbol, symbol)
    if day is None:
        return FnoLevelOut(level=None, close_of=None, live_symbol=live_symbol)
    level = (
        await session.execute(
            select(func.max(FoContractDaily.underlying)).where(
                FoContractDaily.trade_date == day,
                FoContractDaily.symbol == symbol,
                FoContractDaily.option_type == FUTURE,
            )
        )
    ).scalar_one_or_none()
    return FnoLevelOut(
        level=level, close_of=day if level is not None else None, live_symbol=live_symbol
    )


async def paper_tally(session: AsyncSession, user_id: int) -> list[FnoTallyOut]:
    """``04`` §9 against the paper journal. Counts only; the checklist's violations are FO10's."""
    out: list[FnoTallyOut] = []
    for sleeve in (FoSleeve.F1N, FoSleeve.F1B, FoSleeve.F2):
        row = (
            await session.execute(
                select(
                    func.count(),
                    func.count(func.distinct(func.date_trunc("month", FoJournal.opened_on))),
                    func.coalesce(func.sum(FoJournal.rolls), 0),
                    func.min(FoJournal.opened_on),
                ).where(
                    FoJournal.user_id == user_id,
                    FoJournal.sleeve == sleeve.value,
                    FoJournal.simulated.is_(True),
                )
            )
        ).one()
        closed, cycles, rolls, first = int(row[0]), int(row[1]), int(row[2]), row[3]
        target = (
            f"{F1_PAPER_CYCLES_PER_UNDERLYING} consecutive monthly cycles, "
            f"≥ {F1_PAPER_MIN_OPENED} opened across F1"
            if group_of(sleeve) is FoSleeveGroup.F1
            else (
                f"{F2_PAPER_SESSIONS} consecutive sessions, ≥ {F2_PAPER_MIN_CLOSED} closed, "
                f"≥ {F2_PAPER_MIN_ROLLS} rolls"
            )
        )
        out.append(
            FnoTallyOut(
                sleeve=sleeve.value,
                target=target,
                closed=closed,
                opened_cycles=cycles,
                rolls=rolls,
                first_opened=first,
                violations=None,
            )
        )
    return out


# --- the information page (``04`` §5) ------------------------------------------------------------


async def latest_series_date(session: AsyncSession) -> dt.date | None:
    return (
        await session.execute(select(func.max(FoUnderlyingDaily.trade_date)))
    ).scalar_one_or_none()


async def _near_monthlies(session: AsyncSession, after: dt.date) -> dict[str, tuple[dt.date, int]]:
    """``symbol -> (first monthly strictly after ``after``, its lot)`` from ``op_expiry``,
    withdrawn expiries excluded; the monthly is the month's last listed expiry (``04`` §1)."""
    rows = await session.execute(
        select(OpExpiry.underlying, OpExpiry.expiry_date, OpExpiry.lot_size, OpExpiry.detail)
    )
    listed: dict[str, dict[dt.date, int]] = {}
    for underlying, expiry, lot, detail in rows:
        if "withdrawn_on" in (detail or {}):
            continue
        listed.setdefault(str(underlying), {})[expiry] = int(lot)
    out: dict[str, tuple[dt.date, int]] = {}
    for symbol, lots in listed.items():
        ahead = [d for d in monthly_expiries(lots) if d > after]
        if ahead:
            out[symbol] = (ahead[0], lots[ahead[0]])
    return out


async def _future_lots(session: AsyncSession, day: dt.date) -> dict[str, int]:
    """The bhavcopy's lot per symbol on ``day`` — the fallback when the master has no monthly."""
    rows = await session.execute(
        select(FoContractDaily.symbol, func.max(FoContractDaily.lot_size))
        .where(FoContractDaily.trade_date == day, FoContractDaily.option_type == FUTURE)
        .group_by(FoContractDaily.symbol)
    )
    return {str(s): int(lot) for s, lot in rows if lot is not None}


async def _iv_percentiles(session: AsyncSession, day: dt.date) -> dict[str, tuple[int, int]]:
    """``symbol -> (sessions with an iv_atm in the year to ``day``, of which ≤ day's iv)``."""
    today = (
        select(FoUnderlyingDaily.symbol.label("symbol"), FoUnderlyingDaily.iv_atm.label("iv"))
        .where(FoUnderlyingDaily.trade_date == day, FoUnderlyingDaily.iv_atm.is_not(None))
        .subquery()
    )
    hist = FoUnderlyingDaily
    rows = await session.execute(
        select(
            today.c.symbol,
            func.count(),
            func.count().filter(hist.iv_atm <= today.c.iv),
        )
        .join(hist, hist.symbol == today.c.symbol)
        .where(
            hist.trade_date > day - dt.timedelta(days=IV_PERCENTILE_WINDOW_DAYS),
            hist.trade_date <= day,
            hist.iv_atm.is_not(None),
        )
        .group_by(today.c.symbol)
    )
    return {str(s): (int(n), int(le)) for s, n, le in rows}


async def _oi_five_back(session: AsyncSession, day: dt.date) -> dict[str, int]:
    """``symbol -> oi_total`` on the session five stored sessions before ``day``."""
    ranked = (
        select(
            FoUnderlyingDaily.symbol.label("symbol"),
            FoUnderlyingDaily.oi_total.label("oi"),
            func.row_number()
            .over(
                partition_by=FoUnderlyingDaily.symbol,
                order_by=FoUnderlyingDaily.trade_date.desc(),
            )
            .label("rn"),
        )
        .where(
            FoUnderlyingDaily.trade_date <= day,
            FoUnderlyingDaily.trade_date > day - dt.timedelta(days=30),
        )
        .subquery()
    )
    rows = await session.execute(
        select(ranked.c.symbol, ranked.c.oi).where(ranked.c.rn == OI_CHANGE_SESSIONS + 1)
    )
    return {str(s): int(oi) for s, oi in rows if oi is not None}


async def info_rows(session: AsyncSession, day: dt.date) -> list[FnoInfoRowOut]:
    """Every F&O underlying on ``day``, sorted by futures turnover (``04`` §5's default sort)."""
    series = list(
        (
            await session.execute(
                select(FoUnderlyingDaily).where(FoUnderlyingDaily.trade_date == day)
            )
        ).scalars()
    )
    monthlies = await _near_monthlies(session, day)
    lots = await _future_lots(session, day)
    ivp = await _iv_percentiles(session, day)
    oi_back = await _oi_five_back(session, day)
    out: list[FnoInfoRowOut] = []
    for r in series:
        near = monthlies.get(r.symbol)
        n_iv, le = ivp.get(r.symbol, (0, 0))
        back = oi_back.get(r.symbol)
        oi_change = (
            _q(Decimal(r.oi_total - back) * 100 / Decimal(back), "0.01")
            if r.oi_total is not None and back
            else None
        )
        iv_rv = (
            _q(r.iv_atm / r.rv20, "0.01")
            if r.iv_atm is not None and r.rv20 is not None and r.rv20 > 0
            else None
        )
        out.append(
            FnoInfoRowOut(
                symbol=r.symbol,
                lot_size=near[1] if near else lots.get(r.symbol),
                near_monthly=near[0] if near else None,
                days_to_near_monthly=(near[0] - day).days if near else None,
                fut_settle=r.level_c,
                basis_ann=r.basis_ann,
                oi_change_5d_pct=oi_change,
                iv=r.iv_atm,
                rv20=r.rv20,
                iv_rv=iv_rv,
                iv_pct_1y=_q(Decimal(le) * 100 / Decimal(n_iv), "0.1")
                if n_iv >= IV_PERCENTILE_MIN_SESSIONS
                else None,
                iv_sessions_1y=n_iv,
                in_ban=r.in_ban,
                fut_turnover_20d=r.fut_turnover_20d,
            )
        )
    out.sort(key=lambda row: (-(row.fut_turnover_20d or Decimal(0)), row.symbol))
    return out


def families(backtests: Sequence[FnoBacktestOut]) -> list[FnoFamilyOut]:
    """The verdict table with the newest re-test of any of each row's families beside it."""
    by_family = {b.family: b for b in backtests}
    out: list[FnoFamilyOut] = []
    for row in VERDICT_TABLE:
        runs = [by_family[c] for c in row.retest_families if c in by_family]
        out.append(
            FnoFamilyOut(
                verdict=row,
                latest_retest=max(runs, key=lambda b: b.run_at) if runs else None,
            )
        )
    return out


async def spread_summary(session: AsyncSession) -> FnoSpreadOut:
    row = (
        await session.execute(
            select(
                func.count(func.distinct(FoSpreadSample.trade_date)),
                func.min(FoSpreadSample.trade_date),
                func.max(FoSpreadSample.trade_date),
                func.count(func.distinct(FoSpreadSample.symbol)),
            )
        )
    ).one()
    return FnoSpreadOut(sessions=int(row[0]), first=row[1], last=row[2], symbols=int(row[3]))


async def ingest_summary(session: AsyncSession, today: dt.date) -> FnoIngestOut:
    latest = (
        await session.execute(select(FoIngestDay).order_by(FoIngestDay.trade_date.desc()).limit(1))
    ).scalar_one_or_none()
    missing = (
        await session.execute(
            select(FoIngestDay.trade_date)
            .where(
                FoIngestDay.status == "MISSING",
                FoIngestDay.trade_date >= today - dt.timedelta(days=MISSING_LOOKBACK_DAYS),
            )
            .order_by(FoIngestDay.trade_date.desc())
        )
    ).scalars()
    ban = (
        await session.execute(
            select(FoIngestDay)
            .where(FoIngestDay.ban_for_session.is_not(None))
            .order_by(FoIngestDay.trade_date.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return FnoIngestOut(
        latest_date=None if latest is None else latest.trade_date,
        latest_status=None if latest is None else latest.status,
        missing_days=list(missing),
        ban_for_session=None if ban is None else ban.ban_for_session,
        ban_symbols=[] if ban is None else sorted(ban.ban_symbols or []),
    )
