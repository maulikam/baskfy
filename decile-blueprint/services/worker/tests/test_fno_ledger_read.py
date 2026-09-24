"""FO10: the ledger, pause and paper-checklist readers FO5's API will call (``docs/fno/06`` FO10).

Against the test Postgres, inside a transaction that is rolled back. Dates sit in 2031 so
nothing collides with committed rows. What is asserted is ``03`` §6 and ``04`` §7/§9: figures
per ``(sleeve, underlying, simulated)`` and per book per ``simulated``, never pooled; the F1
pause stands until lifted; the checklist counts every violation kind the tables can prove.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Final

import pytest
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from baskfy_core.fno.config import FoSleeve
from baskfy_core.fno.ledger import PauseCode
from baskfy_core.models import (
    AppUser,
    Exchange,
    FoFill,
    FoIngestDay,
    FoJournal,
    FoLeg,
    FoMark,
    FoPlan,
    FoPosition,
    FoScan,
    TradingDay,
)
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_worker.fno.ledger import (
    lift_f1_pause,
    read_checklist,
    read_ledger,
    read_pauses,
)

ENV_VAR: Final = "BASKFY_TEST_DATABASE_URL"
IST: Final = dt.timezone(dt.timedelta(hours=5, minutes=30))
JUL: Final = [dt.date(2031, 7, d) for d in range(1, 32) if dt.date(2031, 7, d).weekday() < 5]

requires_db = pytest.mark.db(
    pytest.mark.skipif(os.environ.get(ENV_VAR) is None, reason=f"{ENV_VAR} is not set")
)


@asynccontextmanager
async def _rolled_back() -> AsyncIterator[AsyncSession]:
    url = os.environ[ENV_VAR]
    engine = create_async_engine(url)
    session = async_sessionmaker(engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        await session.rollback()
        await session.close()
        await engine.dispose()


async def _user(session: AsyncSession) -> int:
    user = AppUser(public_id="fo10ledger01", email="fo10-ledger@example.com", name="FO10")
    session.add(user)
    await session.flush()
    return int(user.id)


async def _calendar(session: AsyncSession) -> None:
    await session.execute(
        insert(Exchange).values(id=NSE_EXCHANGE_ID, code="NSE").on_conflict_do_nothing()
    )
    await session.execute(
        insert(TradingDay)
        .values(
            [
                {
                    "exchange_id": NSE_EXCHANGE_ID,
                    "date": d,
                    "is_trading_day": True,
                    "source": "derived",
                }
                for d in JUL
            ]
        )
        .on_conflict_do_nothing()
    )


def _at(day: dt.date, hour: int = 15) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour), tzinfo=IST)


async def _position(  # noqa: PLR0913 - one position, by keyword
    session: AsyncSession,
    user: int,
    *,
    sleeve: str,
    opened: dt.date,
    closed: dt.date | None = None,
    simulated: bool = True,
    expiry: dt.date | None = None,
    entry_plan_id: str | None = None,
) -> int:
    symbol = {"F1N": "NIFTY", "F1B": "BANKNIFTY"}.get(sleeve, "ZQA")
    legs = [] if expiry is None else [{"expiry": expiry.isoformat(), "role": "LONG_FUTURE"}]
    position = FoPosition(
        user_id=user,
        sleeve=sleeve,
        symbol=symbol,
        structure="FUTURE" if sleeve == "F2" else "IRON_CONDOR",
        entry_plan_id=entry_plan_id or f"seed-{sleeve}-{opened}-{closed}-{simulated}",
        legs={"legs": legs},
        lots=1,
        lot_size=75,
        max_loss_inr=Decimal(25000),
        opened_at=_at(opened, 10),
        closed_at=None if closed is None else _at(closed),
        closed_reason=None if closed is None else "LOSS_CLOSE",
        simulated=simulated,
    )
    session.add(position)
    await session.flush()
    return int(position.id)


async def _closed(  # noqa: PLR0913 - one closed trade, by keyword
    session: AsyncSession,
    user: int,
    *,
    sleeve: str,
    r: str,
    net: str,
    on: dt.date,
    simulated: bool = True,
) -> int:
    pid = await _position(
        session, user, sleeve=sleeve, opened=on - dt.timedelta(days=1), closed=on,
        simulated=simulated,
    )  # fmt: skip
    session.add(
        FoJournal(
            position_id=pid,
            user_id=user,
            sleeve=sleeve,
            symbol={"F1N": "NIFTY", "F1B": "BANKNIFTY"}.get(sleeve, "ZQA"),
            structure="FUTURE" if sleeve == "F2" else "IRON_CONDOR",
            opened_on=on - dt.timedelta(days=1),
            closed_on=on,
            entry_inr=Decimal(0),
            exit_inr=Decimal(0),
            gross_pnl_inr=Decimal(net),
            costs_inr=Decimal(0),
            net_pnl_inr=Decimal(net),
            risk_budget_inr=Decimal(25000),
            r_multiple=Decimal(r),
            closed_reason="LOSS_CLOSE",
            sessions_held=1,
            simulated=simulated,
            sizing_mode="BUDGET",
        )
    )
    await session.flush()
    return pid


def _plan(
    user: int, plan_id: str, sleeve: str, day: dt.date, *, detail: dict[str, object]
) -> FoPlan:
    return FoPlan(
        user_id=user,
        plan_id=plan_id,
        sleeve=sleeve,
        symbol={"F1N": "NIFTY", "F1B": "BANKNIFTY"}.get(sleeve, "ZQA"),
        trade_date=day,
        structure="FUTURE" if sleeve == "F2" else "IRON_CONDOR",
        kind="ENTRY",
        sizing_mode="BUDGET",
        issued_at=_at(day, 9),
        expires_at=_at(day, 10),
        lots=1,
        lot_size=75,
        status="CLOSED",
        detail=detail,
    )


@requires_db
class TestLedger:
    async def test_per_line_and_per_book_never_pooled(self) -> None:
        async with _rolled_back() as session:
            user = await _user(session)
            await _closed(session, user, sleeve="F1N", r="0.40", net="10000", on=JUL[1])
            await _closed(session, user, sleeve="F1N", r="-0.70", net="-17500", on=JUL[3])
            await _closed(session, user, sleeve="F1N", r="-1", net="-25000", on=JUL[4],
                          simulated=False)  # fmt: skip
            open_pid = await _position(session, user, sleeve="F2", opened=JUL[5])
            for day, pnl in ((JUL[5], "1200"), (JUL[6], "-300")):
                session.add(FoMark(position_id=open_pid, trade_date=day, user_id=user,
                                   mark_points=Decimal(1), pnl_inr=Decimal(pnl)))  # fmt: skip
            await session.flush()
            ledger = await read_ledger(session, user, JUL[10])
            paper = ledger.line(FoSleeve.F1N, "NIFTY", simulated=True)
            live = ledger.line(FoSleeve.F1N, "NIFTY", simulated=False)
            assert paper is not None and live is not None
            assert (paper.closed, paper.realised_inr, paper.realised_r) == (
                2, Decimal("-7500.00"), Decimal("-0.30"))  # fmt: skip
            assert paper.max_drawdown_inr == Decimal("17500.00")
            assert (live.closed, live.realised_inr) == (1, Decimal("-25000.00"))
            book = ledger.book(simulated=True)
            assert book is not None
            assert book.open_marked_inr == Decimal("-300.00")  # the latest mark only
            assert book.total_inr == Decimal("-7800.00")
            live_book = ledger.book(simulated=False)
            assert live_book is not None and live_book.realised_inr == Decimal("-25000.00")


@requires_db
class TestPauses:
    async def test_the_f1_run_is_per_mode_and_stands_until_lifted(self) -> None:
        async with _rolled_back() as session:
            user = await _user(session)
            # Closed before the lift's clock (the audit row is stamped now()), so a past July.
            past = [dt.date(2025, 7, d) for d in (1, 2, 3)]
            for day in past:
                await _closed(session, user, sleeve="F1N", r="-0.60", net="-15000", on=day)
            paper = await read_pauses(session, user, dt.date(2025, 7, 10), simulated=True)
            assert [(p.scope, p.code) for p in paper] == [("F1N", PauseCode.F1_LOSS_RUN)]
            assert await read_pauses(session, user, dt.date(2025, 7, 10), simulated=False) == ()
            await lift_f1_pause(session, user, FoSleeve.F1N, changed_by="maulik", note="review")
            await session.flush()
            assert await read_pauses(session, user, dt.date(2025, 7, 10), simulated=True) == ()
            with pytest.raises(ValueError, match="F1 underlying"):
                await lift_f1_pause(session, user, FoSleeve.F2, changed_by="m", note="x")


@requires_db
class TestChecklist:
    async def test_every_violation_the_tables_can_prove(self) -> None:
        async with _rolled_back() as session:
            user = await _user(session)
            await _calendar(session)
            # A candidate cycle with no plan: MISSED_CYCLE.
            session.add(FoScan(user_id=user, sleeve="F1N", trade_date=JUL[2], symbol="NIFTY",
                               state="CANDIDATE", reasons=["candidate"],
                               detail={"entry_session": JUL[3].isoformat()}))  # fmt: skip
            # A cycle opened: a plan and its position (held to expiry: INTO_EXPIRY).
            session.add(_plan(user, "F1B-opened", "F1B", JUL[3], detail={"violations": [
                {"code": "LATE_EXIT", "message": "passed while down", "at": _at(JUL[8])
                 .isoformat()}]}))  # fmt: skip
            session.add(FoScan(user_id=user, sleeve="F1B", trade_date=JUL[2], symbol="BANKNIFTY",
                               state="CANDIDATE", reasons=["candidate"],
                               detail={"entry_session": JUL[3].isoformat()}))  # fmt: skip
            await session.flush()
            opened = await _position(session, user, sleeve="F1B", opened=JUL[3], closed=JUL[8],
                                     expiry=JUL[8], entry_plan_id="F1B-opened")  # fmt: skip
            # Closed with no journal row: JOURNAL_GAP. Open over two landed nights with one
            # mark: a JOURNAL_GAP for the other.
            await _position(session, user, sleeve="F2", opened=JUL[0], closed=JUL[1])
            carried = await _position(session, user, sleeve="F2", opened=JUL[4])
            for day in (JUL[4], JUL[5]):
                await session.execute(insert(FoIngestDay).values(trade_date=day, status="INGESTED")
                                      .on_conflict_do_nothing())  # fmt: skip
            for pid, days in ((carried, (JUL[4],)), (opened, (JUL[4], JUL[5]))):
                for day in days:
                    session.add(FoMark(position_id=pid, trade_date=day, user_id=user,
                                       mark_points=Decimal(1), pnl_inr=Decimal(0)))  # fmt: skip
            # A short sold before its long: UNCOVERED_SHORT.
            plan = _plan(user, "F1B-opened-legs", "F1B", JUL[3], detail={})
            session.add(plan)
            await session.flush()
            legs = {}
            for seq, role, side in ((1, "SHORT_CALL", "SELL"), (2, "LONG_CALL", "BUY")):
                leg = FoLeg(user_id=user, plan_id=plan.id, entry_seq=seq, role=role,
                            tradingsymbol=f"BANKNIFTYZQ{role}", instrument_token=seq,
                            expiry=JUL[8], option_type="CE", side=side, quantity=30)  # fmt: skip
                session.add(leg)
                await session.flush()
                legs[role] = leg
            for n, role in enumerate(("SHORT_CALL", "LONG_CALL")):
                session.add(FoFill(user_id=user, leg_id=legs[role].id, position_id=opened,
                                   filled_at=_at(JUL[3], 9) + dt.timedelta(minutes=n),
                                   side=legs[role].side, quantity=30, price=Decimal(10),
                                   simulated=True, sim_method="DEPTH_LADDER"))  # fmt: skip
            await session.flush()
            tally = await read_checklist(session, user, JUL[6])
            assert tally.violations_by_kind() == {
                "JOURNAL_GAP": 2,
                "MISSED_CYCLE": 1,
                "UNCOVERED_SHORT": 1,
            }
            later = await read_checklist(session, user, JUL[10])
            assert later.violations_by_kind()["INTO_EXPIRY"] == 1
            assert later.violations_by_kind()["LATE_EXIT"] == 1
            assert later.violations_by_kind()["JOURNAL_GAP"] == 3  # F1B closed unjournalled too
            assert not later.f1_met and not later.f2_met
